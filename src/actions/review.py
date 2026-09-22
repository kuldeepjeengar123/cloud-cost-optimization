"""Human-in-the-loop review — ask before applying recommended changes.

After the pipeline produces its recommendations, this walks the *executable*
ones and asks an operator to approve each before it touches anything — the same
"do you want to proceed?" gate Claude Code uses before acting.

The *decision* is delegated to a ``decide(action) -> Decision`` callback so the
apply / persist / save logic stays identical no matter where the answer comes
from: an interactive terminal prompt today, a web confirm dialog or ChatOps
button tomorrow. *How* an approved change lands is owned by the executor behind
``cfg.action_backend`` — CSV row annotation now (written to a fresh folder),
real AWS calls once the AWS backend is wired in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from ..config import PipelineConfig
from ..utils.logger import get_logger
from .executor import get_executor
from .models import Action
from .store import ActionStore

log = get_logger("actions.review")

# Decisions a ``decide`` callback may return.
APPLY = "apply"   # apply this one
SKIP = "skip"     # leave this one pending, move on
ALL = "all"       # apply this one and every remaining one without asking again
QUIT = "quit"     # stop now; leave the rest pending

Decision = str
DecideFn = Callable[[Action], Decision]
EmitFn = Callable[[str], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def review_and_apply(
    cfg: PipelineConfig,
    actions: list[Action],
    decide: DecideFn,
    emit: EmitFn = print,
    store: ActionStore | None = None,
) -> dict:
    """Walk executable actions, ask ``decide`` for each, apply the approved ones.

    Returns a summary dict and writes ``applied_summary.{json,md}`` into the
    run's folder under ``cfg.applied_folder``.
    """
    store = store or ActionStore(cfg.output_folder / "pending_actions.json")
    executor = get_executor(cfg)

    executable = [a for a in actions if a.executable and a.status == "pending"]
    manual = [a for a in actions if not a.executable]
    run_id = actions[0].run_id if actions else "adhoc"

    applied: list[Action] = []
    failed: list[Action] = []
    skipped: list[Action] = []

    if not executable:
        emit("No executable recommendations to apply (manual items only).")

    approve_all = False
    for action in executable:
        decision = APPLY if approve_all else decide(action)

        if decision == QUIT:
            emit("Stopping review — remaining recommendations left pending.")
            break
        if decision == ALL:
            approve_all = True
            decision = APPLY
        if decision == SKIP:
            skipped.append(action)
            continue

        result = executor.apply(action)
        store.save(result.action)
        emit(f"  [{'OK' if result.ok else 'FAILED'}] {result.message}")
        (applied if result.ok else failed).append(result.action)

    summary = _write_summary(cfg, run_id, applied, failed, skipped, manual)
    emit(
        f"\nApplied {len(applied)} change(s), {len(failed)} failed, "
        f"{len(skipped)} skipped. Details: {summary['summary_path']}"
    )
    return summary


def _write_summary(
    cfg: PipelineConfig,
    run_id: str,
    applied: list[Action],
    failed: list[Action],
    skipped: list[Action],
    manual: list[Action],
) -> dict:
    folder = cfg.applied_folder / run_id
    folder.mkdir(parents=True, exist_ok=True)

    payload = {
        "run_id": run_id,
        "backend": cfg.action_backend,
        "generated_at": _now(),
        "applied": [_describe(a) for a in applied],
        "failed": [_describe(a) for a in failed],
        "skipped": [_describe(a) for a in skipped],
        "manual": [_describe(a) for a in manual],
    }
    json_path = folder / "applied_summary.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    md = [
        f"# Applied changes — {run_id}",
        "",
        f"- Backend: `{cfg.action_backend}`",
        f"- Generated: {payload['generated_at']}",
        f"- Output folder: `{folder}`",
        "",
        f"## Applied ({len(applied)})",
        *(f"- {a.title} — {a.result_note}" for a in applied),
        "",
        f"## Failed ({len(failed)})",
        *(f"- {a.title} — {a.result_note}" for a in failed),
        "",
        f"## Skipped ({len(skipped)})",
        *(f"- {a.title}" for a in skipped),
        "",
        f"## Manual / not auto-applyable ({len(manual)})",
        *(f"- {a.title}" for a in manual),
        "",
    ]
    md_path = folder / "applied_summary.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    return {
        "folder": str(folder),
        "summary_path": str(md_path),
        "applied": len(applied),
        "failed": len(failed),
        "skipped": len(skipped),
    }


def _describe(a: Action) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "impact": a.impact,
        "table": a.table,
        "match_column": a.match_column,
        "match_value": a.match_value,
        "set_fields": a.set_fields,
        "status": a.status,
        "result_note": a.result_note,
    }
