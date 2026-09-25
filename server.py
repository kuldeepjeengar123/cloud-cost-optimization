"""Tiny stdlib-only web server that exposes the pipeline as Server-Sent Events.

Endpoints
---------
GET  /                  -> serves web/index.html
GET  /<asset>           -> serves files from web/ (CSS, JS)
POST /api/run           -> kicks off a pipeline run, streams SSE events:
                              event: step_start | step_complete | final | error
                              data:  JSON payload
POST /api/chat          -> {"question": str, "role": "employee"|"re_team"} -> {"answer", "citations",
                              "insights_file", "mode"}, answered against the most recent run's
                              insights (no re-run); "role" steers framing, see src/chat/answer.py
POST /api/chat/stream   -> same inputs as /api/chat, streamed as SSE events:
                              event: meta | delta | done | error
                              data:  JSON payload (see src/chat/answer.py)
GET  /api/chat/status   -> {"mode", "pipeline_running", "insights_file"} for the chat widget
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Force UTF-8 on Windows console so LLM unicode output prints cleanly
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from src.actions import (
    ActionStore,
    ApprovalError,
    DecisionLogStore,
    commit_batch,
    commit_status,
    get_executor,
    raise_for_review,
    reopen,
    rollback_batch,
    stage_decision,
    unstage,
    withdraw,
)
from src.actions.models import STATUS_DISMISSED
from src.chat import answer_question, stream_answer_question
from src.chat.memory import AnswerCache, ChatHistoryStore
from src.config import load_config
from src.orchestrator import run_pipeline
from src.utils.logger import get_logger

log = get_logger("server")
ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"

# Shared config + action store for the apply loop (the report card's buttons
# link back here). Built once; /api/run still builds a per-request config.
APP_CFG = load_config()
ACTION_STORE = ActionStore(APP_CFG.output_folder / "pending_actions.json")
# Two-stage approval (employee raises, RE team decides) decision history —
# used by the finops_approval_prototype.html / approval_dashboard.html pages.
DECISION_LOG = DecisionLogStore(APP_CFG.output_folder / "decision_log.json")
# Chat widget: Redis answer cache (falls back to in-process) + local SQLite
# question log. Shared across requests so the cache actually caches.
CHAT_CACHE = AnswerCache(APP_CFG.redis_url)
CHAT_HISTORY = ChatHistoryStore(APP_CFG.chat_db_path)
# Flipped around run_pipeline() so /api/chat knows to stay in "info" mode
# while a run is in flight, instead of answering against stale insights.
PIPELINE_RUNNING = threading.Event()

# `target` is the dashboard's single source-selector dropdown (Local files /
# EC2 Rightsizing / Real AWS) — mapping it to sources+backend lives here, in
# one place, instead of the client having to know what each target implies.
# Older callers (web/app.js) that still send sources/backend directly keep
# working unchanged (see _handle_run's fallback).
TARGET_MAP = {
    "local_csv": {"sources": ["local_csv"], "backend": "csv"},
    "ec2_rightsizing": {"sources": ["local_csv"], "backend": "aws"},
    "real_aws": {"sources": ["cost_explorer", "cloudwatch"], "backend": "aws"},
}
# Query override for a target whose analysis should focus on something more
# specific than the default broad cost review. Deliberately names no instance
# type — it steers the LLM's attention at the same by-instance-type
# correlation/anomaly data every run already gets; which instance type (and
# what to resize it to) is the LLM's own read of that data, not this string.
TARGET_QUERY = {
    "ec2_rightsizing": (
        "Analyze EC2 cost by instance type and identify any instance type "
        "showing signs of being undersized (e.g. cost spikes consistent with "
        "CPU credit exhaustion on a burstable instance). Recommend the "
        "specific instance type change needed to resolve it."
    ),
}
# The target the *last completed run* actually used — see _handle_get_target.
# Tracked separately from APP_CFG.action_backend because backend "aws" alone
# is ambiguous between "ec2_rightsizing" and "real_aws" (they differ only in
# `sources`).
LAST_TARGET = "local_csv"

# One AWS client for the whole process. Its boto3 clients are built lazily
# and then cached on the instance, and that cold start — credential-chain
# resolution, TLS handshake, the STS account lookup list_budgets needs — is
# ~3-4s, against ~0.25s for the same calls once warm. Building a fresh
# RealAWSClient per request (as this handler used to) paid that cold start
# on every single fleet read, which is most of why the dashboard's buttons
# felt slow. Region comes from the environment and never changes at runtime,
# so a plain singleton is enough; botocore clients are safe to share across
# the ThreadingHTTPServer's threads.
_aws_client = None
_aws_client_lock = threading.Lock()


def _shared_aws_client():
    global _aws_client
    with _aws_client_lock:
        if _aws_client is None:
            from src.aws.real_client import RealAWSClient

            _aws_client = RealAWSClient(APP_CFG)
        return _aws_client


# /api/aws/fleet is re-fetched by the dashboard's loadState() after nearly
# every button click (accept/reject/stage/commit/reset...). Even warm, that's
# a real network round trip, so hold the last good read briefly: a click that
# actually changes the fleet (commit/rollback) invalidates it immediately,
# and the Refresh button forces a fresh read, so nothing goes stale unnoticed.
_FLEET_CACHE_TTL = 30.0  # seconds
_fleet_cache_lock = threading.Lock()
_fleet_cache = {"ts": 0.0, "payload": None}


def _invalidate_fleet_cache() -> None:
    with _fleet_cache_lock:
        _fleet_cache["ts"] = 0.0
        _fleet_cache["payload"] = None

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieten the default access log
        log.info("%s - %s", self.address_string(), fmt % args)

    # ----- routing -----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "":
            return self._serve_file(WEB_DIR / "finops_approval_prototype.html")
        # The original chat-driven pipeline UI now lives at /chat.
        if path == "/chat":
            return self._serve_file(WEB_DIR / "index.html")
        # The "Apply" link from Teams opens this confirmation page.
        if path == "/apply":
            return self._serve_file(WEB_DIR / "apply.html")
        if path == "/api/actions":
            return self._handle_list_actions()
        if path == "/api/action":
            return self._handle_get_action(parse_qs(parsed.query))
        if path == "/api/decisions":
            return self._handle_list_decisions()
        if path == "/api/actions/commit-status":
            return self._handle_commit_status()
        if path == "/api/target":
            return self._handle_get_target()
        if path == "/api/aws/fleet":
            return self._handle_aws_fleet()
        if path == "/api/chat/status":
            return self._handle_chat_status()
        if path.startswith("/api/"):
            return self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "POST only")
        # static asset
        asset = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR not in asset.parents and asset != WEB_DIR:
            return self._send_error(HTTPStatus.FORBIDDEN, "Bad path")
        if not asset.exists() or not asset.is_file():
            return self._send_error(HTTPStatus.NOT_FOUND, "Not found")
        return self._serve_file(asset)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/run":
            return self._handle_run()
        if path == "/api/chat":
            return self._handle_chat()
        if path == "/api/chat/stream":
            return self._handle_chat_stream()
        if path == "/api/apply":
            return self._handle_apply()
        if path == "/api/actions/raise":
            return self._handle_raise()
        if path == "/api/actions/withdraw":
            return self._handle_withdraw()
        if path == "/api/actions/reopen":
            return self._handle_reopen()
        if path == "/api/actions/stage":
            return self._handle_stage()
        if path == "/api/actions/unstage":
            return self._handle_unstage()
        if path == "/api/actions/commit":
            return self._handle_commit()
        if path == "/api/actions/rollback":
            return self._handle_rollback()
        if path == "/api/test/reset":
            return self._handle_test_reset()
        return self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    # ----- handlers -----
    def _handle_run(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}

        target = (body.get("target") or "").strip().lower()
        if target in TARGET_MAP:
            mapped = TARGET_MAP[target]
            sources, backend = mapped["sources"], mapped["backend"]
        else:
            sources = body.get("sources") or ["local_csv"]
            # Backend: explicit from the client, else infer ("aws" when reading
            # from the cost_explorer/cloudwatch sources).
            backend = body.get("backend")
            if not backend:
                backend = "aws" if any(s in ("cost_explorer", "cloudwatch") for s in sources) else "csv"
            # Best-effort target label for callers that skip TARGET_MAP (older
            # web/app.js-style calls) so _handle_get_target still reports something.
            if backend != "aws":
                target = "local_csv"
            elif sources == ["local_csv"]:
                target = "ec2_rightsizing"
            else:
                target = "real_aws"

        query = TARGET_QUERY.get(target) or body.get("query") or (
            "Provide a comprehensive AWS cost analysis with key insights and recommendations."
        )

        date_range = (body.get("date_range") or "").strip().lower() or None
        cfg = load_config(
            user_query=query, sources=sources, action_backend=backend,
            date_range=date_range,
        )
        # Every other handler (stage/commit/apply/rollback/fleet reads) acts
        # against the shared APP_CFG, not this per-run cfg — keep it in sync
        # with whatever target this run selected so an approved change from
        # a "Real AWS" run doesn't silently commit against the wrong backend.
        global LAST_TARGET
        APP_CFG.action_backend = cfg.action_backend
        LAST_TARGET = target

        # A fresh run starts from a clean slate: drop every action (pending,
        # applied, dismissed, declined) left over from earlier runs so the
        # Recommendations list only ever reflects the run in progress. The
        # decision log is untouched — it's the permanent RE-approval audit trail.
        cleared = ACTION_STORE.clear()
        if cleared:
            log.info("Cleared %s action(s) from a previous run before starting a new one", cleared)

        # SSE headers — Connection: close so the client sees EOF after `done`
        # and its fetch() promise can resolve cleanly.
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        q: queue.Queue = queue.Queue()

        def on_event(kind: str, payload: dict):
            q.put((kind, payload))

        def runner():
            PIPELINE_RUNNING.set()
            try:
                run_pipeline(cfg, on_event=on_event)
            except Exception as exc:
                log.exception("Pipeline failed")
                q.put(("error", {"message": str(exc)}))
            finally:
                PIPELINE_RUNNING.clear()
                q.put(("__end__", {}))

        threading.Thread(target=runner, daemon=True).start()

        while True:
            kind, payload = q.get()
            if kind == "__end__":
                try:
                    self._write_sse("done", {})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                break
            try:
                self._write_sse(kind, payload)
            except (BrokenPipeError, ConnectionResetError):
                log.warning("Client disconnected mid-stream")
                return
        # Flush what's buffered; do NOT close wfile ourselves. close_connection
        # (set above) makes the server close the socket after handle() returns,
        # so the browser still gets EOF. Closing wfile here would make the base
        # handler's own trailing flush raise "I/O operation on closed file".
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass

    def _handle_list_actions(self) -> None:
        actions = [a.to_dict() for a in ACTION_STORE.all()]
        # Newest run first, pending before resolved.
        order = {"pending": 0, "applied": 1, "acknowledged": 1, "dismissed": 2}
        actions.sort(key=lambda a: (order.get(a.get("status"), 9), a.get("run_id", "")), reverse=False)
        self._send_json(HTTPStatus.OK, {"actions": actions})

    def _handle_get_action(self, query: dict) -> None:
        action_id = (query.get("id") or [""])[0]
        action = ACTION_STORE.get(action_id)
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})
        self._send_json(HTTPStatus.OK, {"action": action.to_dict()})

    def _handle_get_target(self) -> None:
        """The target the *last completed run* actually used — authoritative
        over whatever a client's own localStorage remembers, since APP_CFG is
        shared across every tab/session hitting this server."""
        self._send_json(HTTPStatus.OK, {
            "target": LAST_TARGET, "action_backend": APP_CFG.action_backend,
        })

    def _handle_aws_fleet(self) -> None:
        """Proxy the live AWS inventory + budgets so the report page can show
        what changed after an apply, same-origin.

        Cached for ``_FLEET_CACHE_TTL`` seconds (see that constant) so a burst
        of dashboard clicks doesn't each pay for a fresh round trip to AWS;
        pass ``?fresh=1`` (the "Refresh" button in live mode) to force one.
        """
        force = parse_qs(urlparse(self.path).query).get("fresh", ["0"])[0] == "1"
        with _fleet_cache_lock:
            cached = _fleet_cache["payload"]
            age = time.monotonic() - _fleet_cache["ts"]
        if cached is not None and age < _FLEET_CACHE_TTL and not force:
            return self._send_json(HTTPStatus.OK, cached)

        client = _shared_aws_client()
        try:
            # describe_instances and list_budgets are independent AWS calls —
            # run them concurrently instead of back-to-back so the request
            # takes as long as the slower of the two, not their sum.
            with ThreadPoolExecutor(max_workers=2) as pool:
                instances_future = pool.submit(lambda: client.describe_instances()["Reservations"][0]["Instances"])
                budgets_future = pool.submit(lambda: client.list_budgets().get("Budgets", []))
                instances = instances_future.result()
                budgets = budgets_future.result()
        except Exception as exc:  # never 500 the dashboard's fleet panel
            log.warning("Fleet read failed: %s", exc)
            payload = {"available": False, "instances": []}
            self._cache_fleet(payload)
            return self._send_json(HTTPStatus.OK, payload)

        running = [i for i in instances if i.get("State", {}).get("Name") == "running"]
        stopped = [i for i in instances if i.get("State", {}).get("Name") == "stopped"]
        payload = {
            "available": True,
            "endpoint": APP_CFG.aws_region,
            "instances": instances,
            "total_count": len(instances),
            "running_count": len(running),
            "stopped_count": len(stopped),
            "monthly_cost": round(sum(i.get("MonthlyCost", 0) for i in running), 2),
            "budgets": budgets,
        }
        self._cache_fleet(payload)
        self._send_json(HTTPStatus.OK, payload)

    @staticmethod
    def _cache_fleet(payload: dict) -> None:
        """Stamp the cache *after* the AWS call returns, not before it starts —
        a read slower than the TTL would otherwise land already expired and
        never serve anyone."""
        with _fleet_cache_lock:
            _fleet_cache["ts"] = time.monotonic()
            _fleet_cache["payload"] = payload

    def _handle_apply(self) -> None:
        """The Teams-card one-click link (``/apply?id=...``).

        This used to apply an executable action immediately — a single click
        reaching AWS with no RE review at all. It now only ever *raises* an
        executable recommendation into the RE queue, exactly like the
        employee dashboard's "Accept" button: real AWS/CSV changes happen
        only through ``commit_batch()`` once the RE team has triaged the
        whole queue. A non-executable (manual) recommendation has nothing to
        raise — it is still just acknowledged in place, as before.
        """
        body = self._read_body()
        action_id = body.get("id") or ""
        decision = (body.get("decision") or "apply").lower()

        action = ACTION_STORE.get(action_id)
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})

        if action.status in ("applied", "dismissed"):
            return self._send_json(
                HTTPStatus.OK,
                {"ok": True, "already": True, "action": action.to_dict(),
                 "message": f"Already {action.status}."},
            )

        if decision == "dismiss":
            action.status = STATUS_DISMISSED
            action.result_note = "Dismissed by user."
            ACTION_STORE.save(action)
            return self._send_json(
                HTTPStatus.OK,
                {"ok": True, "action": action.to_dict(), "message": "Recommendation dismissed."},
            )

        if not action.executable:
            try:
                result = get_executor(APP_CFG, backend=action.backend).apply(action)
            except Exception as exc:  # never 500 the user-facing button
                log.exception("Acknowledge failed")
                return self._send_json(
                    HTTPStatus.OK, {"ok": False, "action": action.to_dict(), "message": str(exc)}
                )
            ACTION_STORE.save(result.action)
            return self._send_json(
                HTTPStatus.OK,
                {"ok": result.ok, "action": result.action.to_dict(), "message": result.message},
            )

        if action.status not in ("pending",):
            return self._send_json(
                HTTPStatus.OK,
                {"ok": True, "already": True, "action": action.to_dict(),
                 "message": f"Already in the approval flow (status: {action.status})."},
            )
        try:
            action = raise_for_review(ACTION_STORE, DECISION_LOG, action, "Teams link")
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "action": action.to_dict(),
             "message": "Sent to the RE team for review — nothing changes in AWS until they approve and commit."},
        )

    def _handle_chat(self) -> None:
        """Answer a free-form follow-up question for the chat widget.

        Grounded in the most recent run's insights once one exists; a plain
        platform-info assistant otherwise (no run yet, or one in progress).
        Never re-runs the pipeline.
        """
        body = self._read_body()
        question = (body.get("question") or "").strip()
        session_id = (body.get("session_id") or "default").strip() or "default"
        role = (body.get("role") or "employee").strip().lower()
        result = answer_question(
            question,
            APP_CFG,
            pipeline_running=PIPELINE_RUNNING.is_set(),
            session_id=session_id,
            role=role,
            actions=[a.to_dict() for a in ACTION_STORE.all()],
            cache=CHAT_CACHE,
            history=CHAT_HISTORY,
        )
        self._send_json(HTTPStatus.OK, result)

    def _handle_chat_stream(self) -> None:
        """SSE variant of /api/chat for the popup widget: streams the answer
        token-by-token (event: meta | delta | done) instead of waiting for
        the full completion. Same mode selection, cache and history as
        /api/chat — see stream_answer_question()."""
        body = self._read_body()
        question = (body.get("question") or "").strip()
        session_id = (body.get("session_id") or "default").strip() or "default"
        role = (body.get("role") or "employee").strip().lower()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            for event, payload in stream_answer_question(
                question,
                APP_CFG,
                pipeline_running=PIPELINE_RUNNING.is_set(),
                session_id=session_id,
                role=role,
                actions=[a.to_dict() for a in ACTION_STORE.all()],
                cache=CHAT_CACHE,
                history=CHAT_HISTORY,
            ):
                self._write_sse(event, payload)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("Client disconnected mid-chat-stream")
            return
        except Exception as exc:
            log.exception("Chat stream failed")
            try:
                self._write_sse("error", {"message": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass

    def _handle_chat_status(self) -> None:
        """Lets the widget know, without asking a question, whether it should
        present itself as info-only or fully grounded in run data."""
        from src.chat.answer import latest_insights_path

        running = PIPELINE_RUNNING.is_set()
        insights_path = None if running else latest_insights_path(APP_CFG)
        self._send_json(HTTPStatus.OK, {
            "mode": "info" if insights_path is None else "rag",
            "pipeline_running": running,
            "insights_file": str(insights_path) if insights_path else None,
        })

    def _handle_list_decisions(self) -> None:
        self._send_json(HTTPStatus.OK, {"decisions": DECISION_LOG.all()})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _handle_raise(self) -> None:
        """Employee accepts a recommendation -> raised to the RE queue."""
        body = self._read_body()
        action = ACTION_STORE.get(body.get("id") or "")
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})
        raised_by = (body.get("raised_by") or "").strip() or "Unknown requester"
        try:
            action = raise_for_review(ACTION_STORE, DECISION_LOG, action, raised_by)
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        self._send_json(HTTPStatus.OK, {"ok": True, "action": action.to_dict()})

    def _handle_withdraw(self) -> None:
        """Employee pulls a raised request back before the RE team decides."""
        body = self._read_body()
        action = ACTION_STORE.get(body.get("id") or "")
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})
        actor = (body.get("actor") or action.raised_by or "Unknown requester")
        try:
            action = withdraw(ACTION_STORE, DECISION_LOG, action, actor)
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        self._send_json(HTTPStatus.OK, {"ok": True, "action": action.to_dict()})

    def _handle_reopen(self) -> None:
        """Bring a declined (or self-dismissed) recommendation back to open."""
        body = self._read_body()
        action = ACTION_STORE.get(body.get("id") or "")
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})
        actor = (body.get("actor") or "Unknown requester")
        try:
            action = reopen(ACTION_STORE, DECISION_LOG, action, actor)
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        self._send_json(HTTPStatus.OK, {"ok": True, "action": action.to_dict()})

    def _require_re_role(self) -> bool:
        """RE-only gate for staging, committing and rolling back changes.

        ``RE_TEAM_TOKEN`` unset (the default, demo/local mode) leaves every
        endpoint open, same as before this change. Once it's set in ``.env``,
        every RE-only call must carry a matching ``X-RE-Token`` header, so an
        employee session (which never has the token) cannot approve, stage,
        commit or roll back a change — only raise or withdraw a request. This
        is a shared-secret gate, not real authentication: a production
        deployment needs SSO/IAM-backed role checks in front of this server,
        not just this header.
        """
        required = APP_CFG.re_team_token
        if not required:
            return True
        supplied = self.headers.get("X-RE-Token", "")
        if supplied == required:
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "RE team credentials required for this action."})
        return False

    def _handle_stage(self) -> None:
        """The RE team's approve/decline *intent* on a queued request.

        Never touches AWS or a CSV — see ``actions.approval.stage_decision``.
        The change only lands once every queued request is staged and the RE
        team explicitly commits the batch (``/api/actions/commit``).
        """
        if not self._require_re_role():
            return
        body = self._read_body()
        action = ACTION_STORE.get(body.get("id") or "")
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})
        decision = (body.get("decision") or "").strip().lower()
        staged_by = (body.get("staged_by") or body.get("decided_by") or "").strip() or "Unknown approver"
        reason = body.get("reason")
        override = bool(body.get("override"))
        try:
            action = stage_decision(ACTION_STORE, DECISION_LOG, action, decision, staged_by, reason, override)
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        self._send_json(HTTPStatus.OK, {"ok": True, "action": action.to_dict()})

    def _handle_unstage(self) -> None:
        if not self._require_re_role():
            return
        body = self._read_body()
        action = ACTION_STORE.get(body.get("id") or "")
        if not action:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown action id"})
        actor = (body.get("actor") or "Unknown approver")
        try:
            action = unstage(ACTION_STORE, DECISION_LOG, action, actor)
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        self._send_json(HTTPStatus.OK, {"ok": True, "action": action.to_dict()})

    def _handle_commit_status(self) -> None:
        self._send_json(HTTPStatus.OK, commit_status(ACTION_STORE))

    def _handle_commit(self) -> None:
        """Apply every staged decision as one batch.

        The only path in this server that ever calls an executor for a
        two-stage action — see ``actions.approval.commit_batch``. Refuses if
        any request is still undecided, so the RE team must have triaged the
        entire queue before anything changes.
        """
        if not self._require_re_role():
            return
        body = self._read_body()
        decided_by = (body.get("decided_by") or "").strip() or "Unknown approver"
        try:
            summary = commit_batch(APP_CFG, ACTION_STORE, DECISION_LOG, decided_by)
        except ApprovalError as exc:
            return self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:  # never 500 the user-facing button
            log.exception("Commit failed")
            return self._send_json(HTTPStatus.OK, {"ok": False, "message": str(exc)})
        _invalidate_fleet_cache()  # a commit may have stopped/resized/terminated real instances
        self._send_json(HTTPStatus.OK, {"ok": True, **summary})

    def _handle_rollback(self) -> None:
        """Undo a previously committed batch — see ``actions.rollback``."""
        if not self._require_re_role():
            return
        body = self._read_body()
        batch_id = (body.get("batch_id") or "").strip()
        actor = (body.get("actor") or "Unknown approver")
        if not batch_id:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "batch_id is required"})
        try:
            summary = rollback_batch(APP_CFG, ACTION_STORE, DECISION_LOG, batch_id, actor)
        except ValueError as exc:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:  # never 500 the user-facing button
            log.exception("Rollback failed")
            return self._send_json(HTTPStatus.OK, {"ok": False, "message": str(exc)})
        _invalidate_fleet_cache()  # a rollback may have reversed a real AWS mutation
        self._send_json(HTTPStatus.OK, {"ok": True, **summary})

    def _handle_test_reset(self) -> None:
        """Test-only: wipe every action and decision so the dashboard goes back
        to a clean slate (no recommendations, no charts) — the same state as
        before any pipeline run — without needing to restart the server.
        """
        cleared = ACTION_STORE.clear()
        DECISION_LOG.clear()

        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "actions_cleared": cleared,
            "message": f"Cleared {cleared} action(s) and the decision log.",
        })

    def _send_json(self, status: HTTPStatus, obj: dict) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(self, event: str, data: dict) -> None:
        body = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def _serve_file(self, path: Path) -> None:
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("Serving on http://127.0.0.1:%s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    main(port)
