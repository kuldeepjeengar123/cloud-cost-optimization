"""Q&A backend for the dashboard's chat widget.

Two modes, chosen automatically per request:

* ``info``  — no pipeline run has completed yet (or one is running right
  now). The assistant can only describe the platform itself: what the
  pipeline does, its five steps, and the approval workflow. It never
  fabricates cost data it doesn't have.
* ``rag``   — a run has completed. The assistant answers grounded in
  whichever ``insights_<timestamp>.json`` ``step5_finalize.py`` wrote most
  recently, same as before — it never calls an input source or re-derives
  numbers, it only reads and reasons over what the pipeline already
  produced.

Both modes share one negative-prompting rule set so the widget can't be
steered into answering questions unrelated to this platform, and both are
cached (``AnswerCache``, Redis-backed with an in-process fallback), given
short-term follow-up context (``ChatContextCache``, also Redis) and logged
(``ChatHistoryStore``, Postgres) via ``src.integrations.chat.memory``. Every
prompt this module sends is loaded from ``src/prompts/*.md`` — see
``src.prompts.load_prompt`` — and composed here with the live, per-request
pieces (the role framing, the approval queue, the dashboard's filters, prior
conversation turns) that can't live in a static file.

Both directions of every turn also pass through
``src.integrations.chat.guardrails``: an incoming question is scrubbed of
anything credential-shaped before it reaches the LLM prompt or is persisted,
and an outgoing answer is scrubbed of this deployment's own known secrets
(plus the same generic credential shapes) before it is cached, logged, or
returned — regardless of mode, and whether streamed or not.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...llm.client import LLMClient, LLMError
from ...prompts import load_prompt
from ...utils.logger import get_logger
from .guardrails import redact_secrets, scrub_user_input
from .memory import AnswerCache, ChatContextCache, ChatHistoryStore

if TYPE_CHECKING:
    from ...config import PipelineConfig

log = get_logger("chat.answer")

MODE_INFO = "info"
MODE_RAG = "rag"

ROLE_EMPLOYEE = "employee"
ROLE_RE_TEAM = "re_team"

ROLE_CONTEXT = {
    ROLE_EMPLOYEE: load_prompt("chat_role_employee"),
    ROLE_RE_TEAM: load_prompt("chat_role_re_team"),
}


def _role_context(role: str) -> str:
    return ROLE_CONTEXT.get(role, ROLE_CONTEXT[ROLE_EMPLOYEE])

PLATFORM_DESCRIPTION = load_prompt("chat_platform_description")

NEGATIVE_PROMPT_RULES = load_prompt("chat_negative_rules")

INFO_SYSTEM_PROMPT = (
    load_prompt("chat_info_intro")
    + "\n\n"
    + NEGATIVE_PROMPT_RULES
    + '\n\nReply with STRICT JSON only: {"answer": str, "citations": []}'
)

RAG_SYSTEM_PROMPT = (
    load_prompt("chat_rag_intro")
    + "\n\n"
    + NEGATIVE_PROMPT_RULES
    + '\n\nReply with STRICT JSON only: {"answer": str, "citations": [str]} where '
    "each citation is a short pointer to which part of the context you used "
    "(e.g. a field or table name)."
)


def _plain_prompt(intro: str) -> str:
    """Same scope rules as the JSON prompts above, but asking for plain prose
    instead — used by the streaming path, where there's no way to hold back
    a structured JSON envelope until it's fully formed without losing the
    token-by-token effect. Citations aren't available in this mode."""
    return (
        intro + "\n\n" + NEGATIVE_PROMPT_RULES
        + "\n\nReply in plain prose only — no JSON, no markdown code fences, no preamble."
    )


INFO_STREAM_SYSTEM_PROMPT = _plain_prompt(load_prompt("chat_info_intro"))

RAG_STREAM_SYSTEM_PROMPT = _plain_prompt(load_prompt("chat_rag_intro"))


def latest_insights_path(cfg: "PipelineConfig") -> Optional[Path]:
    """Public: the widget's status endpoint uses this to report readiness
    without going through ``answer_question`` (which logs/caches a turn)."""
    candidates = sorted(cfg.output_folder.glob("insights_*.json"))
    return candidates[-1] if candidates else None


def _context_slice(payload: dict) -> dict:
    return {
        "business_metadata": payload.get("business_metadata", {}),
        "analysis": payload.get("analysis", {}),
        "summary": payload.get("summary", {}),
        "correlations": {k: v[:10] for k, v in payload.get("correlations", {}).items()},
    }


# Mirrors src.actions.models' STATUS_* string values — duplicated here (as
# plain strings, not an import) so this module doesn't need to depend on the
# actions layer; server.py passes the queue as plain dicts (Action.to_dict()).
_STATUS_LABELS = {
    "pending": "open (not yet raised for approval)",
    "pending_re": "awaiting RE approval",
    "applied": "applied to AWS",
    "declined": "declined by the RE team",
    "dismissed": "rejected by the employee (never sent to the RE team)",
    "acknowledged": "acknowledged (manual, no resource to change)",
}

# Cap on itemized entries per section below, most-recent first — keeps the
# prompt bounded as the queue/history grows over a long-running demo instead
# of silently dropping the newest (most relevant) items off the end.
_MAX_ITEMS_PER_SECTION = 20


def _most_recent(items: list[dict], timestamp_key: str) -> list[dict]:
    return sorted(items, key=lambda a: a.get(timestamp_key) or "", reverse=True)[:_MAX_ITEMS_PER_SECTION]


def _filters_signature(filters: Optional[dict]) -> str:
    """Stable string key for whatever the dashboard's filter state is right
    now — folded into the answer cache key and the conversation-context scope
    so a filter change (Target, date range, env/region/project) is treated
    the same as a new pipeline run: it never reuses a cached or
    previous-turn answer that was about a different selection."""
    return json.dumps(filters or {}, sort_keys=True, default=str)


def _filters_block(filters: Optional[dict], last_target: Optional[str]) -> str:
    """Tells the model exactly what's selected in the dashboard's Target,
    date-range, and fleet filters right now — this is the live UI state, and
    may be ahead of the data below if the user hasn't clicked "Start run"
    since changing it. Without this, the assistant only knows about
    whichever run last completed, so a filter change with no new run yet
    would silently go unmentioned instead of being called out."""
    if not filters:
        return ""
    target = filters.get("target")
    lines = [
        "Dashboard filters currently selected in the UI right now (this is "
        "live, current state — answer about *this*, not about an older "
        "selection from earlier in the conversation):"
    ]
    if target:
        lines.append(f"- Target: {target}")
    if filters.get("date_range"):
        lines.append(f"- Date range: {filters['date_range']}")
    for key, label in (("env", "Environment filter"), ("region", "Region filter"), ("project", "Project filter")):
        val = filters.get(key)
        if val and val != "all":
            lines.append(f"- {label}: {val}")
    if target and last_target and target != last_target:
        lines.append(
            f"- IMPORTANT: the user has selected \"{target}\" in the Target dropdown, but the "
            f"cost data and recommendations available below are still from the last completed "
            f"run, which used \"{last_target}\". Say plainly that the dashboard has not been "
            f"refreshed for the newly selected target yet, and that clicking \"Start run\" will "
            f"do that — don't answer as if the data below already reflects \"{target}\"."
        )
    return "\n".join(lines)


def _context_block(recent_turns: Optional[list[dict]]) -> str:
    """Short "what we already discussed" block from ``ChatContextCache``, so
    a follow-up question ("what about last month?") can be answered without
    the user having to restate what it's a follow-up to. Empty when Redis
    has no context for this session (fresh session, cold cache, or Redis
    unavailable) — the prompt then reads exactly as it did before this was
    added."""
    if not recent_turns:
        return ""
    lines = ["Recent conversation in this session (most recent last — for context only, not a source of cost data):"]
    for turn in recent_turns:
        lines.append(f"- Q: {turn.get('question', '')}\n  A: {turn.get('answer', '')}")
    return "\n".join(lines)


_IST_OFFSET = timedelta(hours=5, minutes=30)


def _fmt_ist(iso: Optional[str]) -> str:
    """Every action timestamp (raised_at, decided_at, ...) is stored as UTC
    — converted here to IST for the prompt so the assistant's answer states
    times the way the dashboard shows them (see fmtTime() in web/shared.js),
    not raw UTC. Falls back to the raw value if it isn't parseable."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return (dt + _IST_OFFSET).strftime("%Y-%m-%d %H:%M IST")


def _actions_summary(actions: Optional[list[dict]], role: str) -> str:
    """Live approval-queue snapshot, independent of any pipeline run's
    insights file — included in every prompt (info or rag mode) so the
    widget can answer "how many requests are in the queue" even before a
    run has ever completed, and so counts (and, for the RE team, individual
    requests) stay current the moment an employee raises one or the RE team
    decides one, rather than only reflecting the last analysis.
    """
    actions = actions or []
    counts: dict[str, int] = {}
    for a in actions:
        status = a.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1

    lines = ["Live approval-queue snapshot (current state, right now — not from a pipeline run):"]
    for status, label in _STATUS_LABELS.items():
        lines.append(f"- {label}: {counts.get(status, 0)}")

    if role == ROLE_RE_TEAM:
        pending = _most_recent([a for a in actions if a.get("status") == "pending_re"], "raised_at")
        if pending:
            lines.append("\nRequests currently awaiting your (RE team) decision:")
            for a in pending:
                lines.append(
                    f"- {a.get('id')}: \"{a.get('title')}\" — impact={a.get('impact')}, "
                    f"risk={a.get('risk_level', 'unknown')}, "
                    f"estimated saving=${(a.get('estimated_savings') or 0):.2f}/mo, "
                    f"raised by {a.get('raised_by') or 'unknown'} at {_fmt_ist(a.get('raised_at'))}"
                )

        decided = _most_recent([a for a in actions if a.get("status") in ("applied", "declined")], "decided_at")
        if decided:
            lines.append("\nRequests you've already decided on:")
            for a in decided:
                if a.get("status") == "applied":
                    lines.append(
                        f"- {a.get('id')}: \"{a.get('title')}\" — APPROVED and applied to AWS by "
                        f"{a.get('decided_by') or 'unknown'} at {_fmt_ist(a.get('decided_at'))}. "
                        f"Result: {a.get('result_note') or 'n/a'}"
                    )
                else:
                    lines.append(
                        f"- {a.get('id')}: \"{a.get('title')}\" — DECLINED by "
                        f"{a.get('decided_by') or 'unknown'} at {_fmt_ist(a.get('decided_at'))}. "
                        f"Reason given to the requester: {a.get('decline_reason') or 'n/a'}"
                    )
    else:
        raised = _most_recent([a for a in actions if a.get("status") in ("pending_re", "applied", "declined")], "raised_at")
        if raised:
            lines.append("\nRequests you've raised and their current status:")
            for a in raised:
                extra = ""
                if a.get("status") == "applied":
                    extra = f" — {a.get('result_note') or ''}".rstrip(" —")
                elif a.get("status") == "declined":
                    extra = f" — declined: {a.get('decline_reason') or 'no reason given'}"
                lines.append(
                    f"- {a.get('id')}: \"{a.get('title')}\" — status={a.get('status')}, "
                    f"estimated saving=${(a.get('estimated_savings') or 0):.2f}/mo{extra}"
                )
    return "\n".join(lines)


def _fallback_reply(mode: str, pipeline_running: bool, role: str = ROLE_EMPLOYEE) -> dict:
    """Used when the LLM is unavailable/unreachable — the widget must always
    say *something* useful rather than error out.

    Marked ``_no_cache`` so a transient failure (LLM rate-limited, briefly
    unreachable, or returned malformed JSON for one request) never gets
    written into ``AnswerCache`` — that would otherwise "lock in" the failure
    for every repeat of the same question for a full CACHE_TTL_SECONDS, even
    after the LLM has recovered. See ``answer_question``/``stream_answer_question``.
    """
    if mode == MODE_RAG:
        return {"answer": "The assistant could not produce a grounded answer for that question. Try rephrasing it.", "citations": [], "_no_cache": True}
    note = " A pipeline run is in progress; full data will be available once it finishes." if pipeline_running else ""
    return {"answer": PLATFORM_DESCRIPTION + "\n\n" + _role_context(role) + note, "citations": [], "_no_cache": True}


def answer_question(
    question: str,
    cfg: "PipelineConfig",
    *,
    pipeline_running: bool = False,
    session_id: str = "default",
    role: str = ROLE_EMPLOYEE,
    actions: Optional[list[dict]] = None,
    cache: Optional[AnswerCache] = None,
    history: Optional[ChatHistoryStore] = None,
    context_cache: Optional[ChatContextCache] = None,
    filters: Optional[dict] = None,
    last_target: Optional[str] = None,
) -> dict:
    """Return ``{"answer", "citations", "insights_file", "mode"}``.

    ``role`` is ``"employee"`` or ``"re_team"`` (see ``ROLE_CONTEXT``) — it
    steers the framing of the answer toward what that role actually sees and
    does on the dashboard, without changing the underlying data or the scope
    rules in ``NEGATIVE_PROMPT_RULES``. ``actions`` is the current approval
    queue (``Action.to_dict()`` for every action the server holds) — live
    state, included regardless of mode; see ``_actions_summary``. ``filters``
    is the dashboard's current Target/date-range/fleet-filter selection (from
    the client, may be ahead of ``last_target`` — the target the last
    completed run actually used — if the user hasn't clicked "Start run"
    since changing it); see ``_filters_block``.

    Never raises for expected conditions (no run yet, no API key, LLM
    failure) — those become a plain-text ``answer`` explaining what happened
    instead of a failed request, matching how the rest of the action API
    never 500s a user-facing button.
    """
    # Scrubbed before it ever reaches the LLM prompt, the cache key, or
    # Postgres history — a user pasting a real credential into the chat box
    # must not have it forwarded to a third-party API or persisted anywhere.
    question = scrub_user_input((question or "").strip())
    role = role if role in ROLE_CONTEXT else ROLE_EMPLOYEE
    insights_path = None if pipeline_running else latest_insights_path(cfg)
    mode = MODE_RAG if insights_path is not None else MODE_INFO
    insights_file = str(insights_path) if insights_path else None

    if not question:
        return {
            "answer": "Ask a question about the platform, its workflow, or (once a run has completed) your AWS cost data.",
            "citations": [], "insights_file": insights_file, "mode": mode,
        }

    # The queue can change between two questions with identical mode/question/
    # role, so it's folded into the cache key too — otherwise a queue-count
    # answer would go stale for up to CACHE_TTL_SECONDS after being asked once.
    # The dashboard's filter selection is folded in the same way: changing
    # Target/date-range/fleet filters must never reuse an answer cached (or a
    # conversation turn kept) under the previous selection.
    queue_snapshot = _actions_summary(actions, role)
    filters_sig = _filters_signature(filters)
    cache_extra = f"{queue_snapshot}\n{filters_sig}"
    # Scopes the conversation-context window to exactly this mode+insights
    # file+filters: once a new pipeline run produces a different insights
    # file, or the user changes a filter, older turns stop counting as
    # context instead of leaking stale (possibly unrelated) data into a
    # fresh answer.
    context_scope = f"{mode}:{insights_file}:{filters_sig}"
    if cache is not None:
        cached = cache.get(mode, insights_file, question, role, cache_extra)
        if cached is not None:
            if context_cache is not None:
                context_cache.add_turn(session_id, question, cached.get("answer", ""), context_scope)
            return {**cached, "insights_file": insights_file, "mode": mode, "cached": True}

    filters_block = _filters_block(filters, last_target)
    convo_context = _context_block(
        context_cache.recent(session_id, context_scope) if context_cache is not None else None
    )
    if mode == MODE_INFO:
        result = _answer_info(question, cfg, pipeline_running, role, queue_snapshot, convo_context, filters_block)
    else:
        result = _answer_rag(question, cfg, insights_path, role, queue_snapshot, convo_context, filters_block)

    # Belt-and-braces: redact this deployment's own secrets (and any
    # generically-shaped credential) out of the model's answer before it is
    # cached, persisted, or returned — see src/integrations/chat/guardrails.py.
    result["answer"] = redact_secrets(result.get("answer", ""), cfg)

    no_cache = result.pop("_no_cache", False)
    if cache is not None and not no_cache:
        cache.set(mode, insights_file, question, result, role, cache_extra)
    if history is not None:
        history.record(session_id, mode, question, result["answer"], insights_file, result["citations"])
    if context_cache is not None:
        context_cache.add_turn(session_id, question, result["answer"], context_scope)

    return {**result, "insights_file": insights_file, "mode": mode}


def _answer_info(
    question: str, cfg: "PipelineConfig", pipeline_running: bool,
    role: str = ROLE_EMPLOYEE, queue_snapshot: str = "", convo_context: str = "", filters_block: str = "",
) -> dict:
    try:
        client = LLMClient(cfg.llm)
    except LLMError:
        return _fallback_reply(MODE_INFO, pipeline_running, role)

    context = PLATFORM_DESCRIPTION
    if pipeline_running:
        context += "\n\n(A pipeline run is currently in progress — no fresh cost data is available yet.)"
    context += "\n\n" + queue_snapshot
    if filters_block:
        context += "\n\n" + filters_block
    if convo_context:
        context += "\n\n" + convo_context
    system_prompt = INFO_SYSTEM_PROMPT + "\n\n" + _role_context(role)
    user_prompt = f"Question: {question}\n\nPlatform description:\n{context}"
    result = client.complete_json(system_prompt, user_prompt, model=cfg.llm.model_chat)
    if not isinstance(result, dict) or not result.get("answer"):
        return _fallback_reply(MODE_INFO, pipeline_running, role)
    return {"answer": str(result.get("answer")), "citations": [str(c) for c in (result.get("citations") or [])]}


def _answer_rag(
    question: str, cfg: "PipelineConfig", path: Path,
    role: str = ROLE_EMPLOYEE, queue_snapshot: str = "", convo_context: str = "", filters_block: str = "",
) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s (%s)", path, exc)
        return {"answer": "The latest run's results could not be read. Try running the analysis again.", "citations": [], "_no_cache": True}

    try:
        client = LLMClient(cfg.llm)
    except LLMError as exc:
        return {"answer": f"Cannot answer right now: {exc}", "citations": [], "_no_cache": True}

    system_prompt = RAG_SYSTEM_PROMPT + "\n\n" + _role_context(role)
    user_prompt = (
        f"Question: {question}\n\nContext (from {path.name}):\n"
        + json.dumps(_context_slice(payload), default=str, indent=2)
        + "\n\n" + queue_snapshot
        + (("\n\n" + filters_block) if filters_block else "")
        + (("\n\n" + convo_context) if convo_context else "")
    )
    result = client.complete_json(system_prompt, user_prompt, model=cfg.llm.model_chat)
    if not isinstance(result, dict) or not result.get("answer"):
        return _fallback_reply(MODE_RAG, False, role)
    return {"answer": str(result.get("answer")), "citations": [str(c) for c in (result.get("citations") or [])]}


def _stream_prompt(
    mode: str, question: str, insights_path: Optional[Path], pipeline_running: bool,
    role: str = ROLE_EMPLOYEE, queue_snapshot: str = "", convo_context: str = "", filters_block: str = "",
) -> tuple[str, str]:
    """Build (system, user) prompts for the streaming path. Raises OSError /
    json.JSONDecodeError the same way ``_answer_rag`` does if the insights
    file can't be read — the caller handles that the same way it handles a
    missing API key."""
    if mode == MODE_INFO:
        context = PLATFORM_DESCRIPTION
        if pipeline_running:
            context += "\n\n(A pipeline run is currently in progress — no fresh cost data is available yet.)"
        context += "\n\n" + queue_snapshot
        if filters_block:
            context += "\n\n" + filters_block
        if convo_context:
            context += "\n\n" + convo_context
        system = INFO_STREAM_SYSTEM_PROMPT + "\n\n" + _role_context(role)
        return system, f"Question: {question}\n\nPlatform description:\n{context}"

    payload = json.loads(insights_path.read_text(encoding="utf-8"))
    system = RAG_STREAM_SYSTEM_PROMPT + "\n\n" + _role_context(role)
    user_prompt = (
        f"Question: {question}\n\nContext (from {insights_path.name}):\n"
        + json.dumps(_context_slice(payload), default=str, indent=2)
        + "\n\n" + queue_snapshot
        + (("\n\n" + filters_block) if filters_block else "")
        + (("\n\n" + convo_context) if convo_context else "")
    )
    return system, user_prompt


def stream_answer_question(
    question: str,
    cfg: "PipelineConfig",
    *,
    pipeline_running: bool = False,
    session_id: str = "default",
    role: str = ROLE_EMPLOYEE,
    actions: Optional[list[dict]] = None,
    cache: Optional[AnswerCache] = None,
    history: Optional[ChatHistoryStore] = None,
    context_cache: Optional[ChatContextCache] = None,
    filters: Optional[dict] = None,
    last_target: Optional[str] = None,
):
    """Generator variant of ``answer_question`` for the popup widget's SSE
    endpoint (``POST /api/chat/stream``).

    Yields ``(event, payload)`` pairs:
      * ``("meta", {"mode", "insights_file", "pipeline_running"})`` — once, first.
      * ``("delta", {"text": str})`` — for each chunk of the answer as it
        streams from the LLM (or the whole cached/fallback answer at once).
      * ``("done", {"answer": str, "citations": [], "cached": bool})`` — once,
        last, with the full assembled answer.

    Citations are always empty here — see ``_plain_prompt``. A cache hit
    still streams as a single ``delta`` so the widget's rendering path stays
    uniform whether or not the LLM was actually called this turn. ``actions``
    is the live approval queue — see ``answer_question``.
    """
    question = scrub_user_input((question or "").strip())
    role = role if role in ROLE_CONTEXT else ROLE_EMPLOYEE
    insights_path = None if pipeline_running else latest_insights_path(cfg)
    mode = MODE_RAG if insights_path is not None else MODE_INFO
    insights_file = str(insights_path) if insights_path else None
    yield "meta", {"mode": mode, "insights_file": insights_file, "pipeline_running": pipeline_running}

    if not question:
        text = "Ask a question about the platform, its workflow, or (once a run has completed) your AWS cost data."
        yield "delta", {"text": text}
        yield "done", {"answer": text, "citations": [], "cached": False}
        return

    queue_snapshot = _actions_summary(actions, role)
    filters_sig = _filters_signature(filters)
    cache_extra = f"{queue_snapshot}\n{filters_sig}"
    context_scope = f"{mode}:{insights_file}:{filters_sig}"
    cached = cache.get(mode, insights_file, question, role, cache_extra) if cache is not None else None
    if cached is not None:
        text = str(cached.get("answer", ""))
        if context_cache is not None:
            context_cache.add_turn(session_id, question, text, context_scope)
        yield "delta", {"text": text}
        yield "done", {"answer": text, "citations": [], "cached": True}
        return

    filters_block = _filters_block(filters, last_target)
    convo_context = _context_block(
        context_cache.recent(session_id, context_scope) if context_cache is not None else None
    )
    chunks: list[str] = []
    # A transient failure (LLM unreachable, rate-limited, an interrupted
    # stream, or one that yielded nothing) must never be cached — that would
    # "lock in" the failure for every repeat of this question for up to
    # CACHE_TTL_SECONDS, even after the LLM has recovered.
    should_cache = True
    try:
        system, user_prompt = _stream_prompt(
            mode, question, insights_path, pipeline_running, role, queue_snapshot, convo_context, filters_block
        )
        client = LLMClient(cfg.llm)
        for delta in client.stream(system, user_prompt, model=cfg.llm.model_chat):
            # Best-effort: catches a secret that lands whole within one
            # chunk. A secret split across a chunk boundary won't be caught
            # here, but the reassembled ``full_text`` below is redacted
            # again before it's cached or persisted, so nothing sensitive
            # is retained even in that edge case.
            delta = redact_secrets(delta, cfg)
            chunks.append(delta)
            yield "delta", {"text": delta}
    except (LLMError, OSError, json.JSONDecodeError) as exc:
        # Setup failure (no API key, unreadable insights file) — nothing streamed yet.
        text = f"Cannot answer right now: {exc}" if mode == MODE_RAG else _fallback_reply(MODE_INFO, pipeline_running, role)["answer"]
        chunks = [text]
        should_cache = False
        yield "delta", {"text": text}
    except Exception as exc:
        # Mid-stream transport failure — some deltas may already be on the wire.
        log.warning("Chat stream interrupted: %s", exc)
        note = "\n\n[Response interrupted — please try asking again.]"
        chunks.append(note)
        should_cache = False
        yield "delta", {"text": note}

    raw_text = "".join(chunks).strip()
    if raw_text:
        full_text = redact_secrets(raw_text, cfg)
    else:
        full_text = _fallback_reply(mode, pipeline_running, role)["answer"]
        should_cache = False
    if cache is not None and should_cache:
        cache.set(mode, insights_file, question, {"answer": full_text, "citations": []}, role, cache_extra)
    if history is not None:
        history.record(session_id, mode, question, full_text, insights_file, [])
    if context_cache is not None:
        context_cache.add_turn(session_id, question, full_text, context_scope)
    yield "done", {"answer": full_text, "citations": [], "cached": False}
