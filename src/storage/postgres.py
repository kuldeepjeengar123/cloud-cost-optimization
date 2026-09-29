"""Durable JSON log of every agent response — chat Q&A, pipeline runs, and
EC2-rightsizing runs — in one Postgres table, ``agent_responses``.

Best-effort, matching the rest of this repo's persistence (see
``src/chat/memory.py``'s Redis cache, ``src/actions/store.py``'s JSON-file
store): a database that's down or misconfigured degrades to "responses
aren't logged", never a broken chat reply or failed pipeline run.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

from ..utils.logger import get_logger

log = get_logger("storage.postgres")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_responses (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT NOT NULL,          -- 'chat' | 'pipeline' | 'ec2_rightsizing'
    session_id TEXT,
    run_id TEXT,
    request JSONB,
    response JSONB NOT NULL,
    cached BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_agent_responses_source_created
    ON agent_responses (source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_responses_session
    ON agent_responses (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_responses_run
    ON agent_responses (run_id) WHERE run_id IS NOT NULL;

-- Mirrors src/actions/decision_log.py's JSON file (outputs/decision_log.json)
-- — the approval dashboard's "Decision activity" panel: one row per
-- raise/stage/approve/decline/withdraw/rollback, same fields as that file's
-- entries. The JSON file stays the source of truth the dashboard reads from
-- (capped at MAX_ENTRIES, newest-first); this table is the durable,
-- unbounded audit trail of the same events.
CREATE TABLE IF NOT EXISTS decision_log (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL,
    action_id TEXT NOT NULL,
    title TEXT,
    decision TEXT NOT NULL,
    actor TEXT,
    estimated_savings DOUBLE PRECISION,
    note TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_log_action_id ON decision_log (action_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_at ON decision_log (at DESC);
"""


class PostgresStore:
    """A fresh connection is opened per call rather than shared: this app's
    web server (``ThreadingHTTPServer``) serves requests concurrently, and
    psycopg connections aren't safe to share across threads.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._lock = threading.Lock()
        self._available = True
        self._ensure_schema()

    def _connect(self):
        import psycopg  # type: ignore

        return psycopg.connect(self.database_url, connect_timeout=3, autocommit=True)

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_SCHEMA)
            log.info("Postgres agent-response store ready")
        except Exception as exc:
            self._available = False
            log.warning("Postgres unavailable (%s); agent responses will not be persisted.", exc)

    def record(
        self,
        *,
        source: str,
        response: dict,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        request: Optional[dict] = None,
        cached: bool = False,
    ) -> None:
        """Insert one row. Never raises — a logging failure must not break
        the chat reply or pipeline run that produced ``response``."""
        if not self._available:
            return
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_responses (source, session_id, run_id, request, response, cached)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        source,
                        session_id,
                        run_id,
                        json.dumps(request, default=str) if request is not None else None,
                        json.dumps(response, default=str),
                        cached,
                    ),
                )
        except Exception as exc:
            log.warning("Could not write agent response to Postgres (%s)", exc)

    def recent(
        self, *, session_id: Optional[str] = None, source: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """Most recent rows, oldest first."""
        if not self._available:
            return []
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, created_at, source, session_id, run_id, request, response, cached
                    FROM agent_responses
                    {where}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            return rows[::-1]
        except Exception as exc:
            log.warning("Could not read agent responses from Postgres (%s)", exc)
            return []

    def record_decision(
        self,
        *,
        at: str,
        action_id: str,
        title: Optional[str],
        decision: str,
        actor: Optional[str],
        estimated_savings: Optional[float],
        note: Optional[str],
    ) -> None:
        """One row per entry in ``DecisionLogStore`` (see
        src/actions/decision_log.py) — never raises, matching ``record()``."""
        if not self._available:
            return
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO decision_log (at, action_id, title, decision, actor, estimated_savings, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (at, action_id, title, decision, actor, estimated_savings, note),
                )
        except Exception as exc:
            log.warning("Could not write decision-log entry to Postgres (%s)", exc)

    def list_decisions(self, *, action_id: Optional[str] = None, limit: int = 500) -> list[dict]:
        """Newest first, mirroring ``DecisionLogStore.all()``."""
        if not self._available:
            return []
        where = "WHERE action_id = %s" if action_id is not None else ""
        params = (action_id,) if action_id is not None else ()
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT at, action_id, title, decision, actor, estimated_savings, note
                    FROM decision_log
                    {where}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            log.warning("Could not read decision log from Postgres (%s)", exc)
            return []
