"""Chat widget persistence: a Redis answer cache, a Redis short-lived
conversation-context window, and a Postgres question/answer log.

All three are best-effort. Redis cuts repeat-question latency and LLM spend
(``AnswerCache``) and gives follow-up questions recent turns to react to
(``ChatContextCache``) — both are pure cache, expected to empty out whenever
Redis restarts (see docker-compose.yml). ``ChatHistoryStore`` is the durable
record of every question asked, in Postgres (``PostgresStore``,
``agent_responses`` table, ``source='chat'``). None of these failure modes
should ever surface as a broken chat reply — matching how the rest of the
action API never 500s a user-facing button.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Optional

from ..storage.postgres import PostgresStore
from ..utils.logger import get_logger

log = get_logger("chat.memory")

CACHE_TTL_SECONDS = 3600
# How many recent turns a session's context window keeps, and how long that
# window survives without a new turn (both far shorter than the permanent
# Postgres record — this is only meant to carry a conversation forward).
CONTEXT_MAX_TURNS = 8
CONTEXT_TTL_SECONDS = 6 * 3600


class AnswerCache:
    """Redis-backed cache for (mode, insights file, question) -> answer.

    Falls back to a small in-process dict — with the same TTL semantics —
    when the ``redis`` package isn't installed or no server is reachable, so
    the widget still works on a machine with no Redis at all.
    """

    def __init__(self, redis_url: str):
        self._client = None
        self._fallback: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._warned = False
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
            client.ping()
            self._client = client
            log.info("Chat answer cache using Redis at %s", redis_url)
        except Exception as exc:
            log.warning("Redis unavailable (%s); chat cache will use in-process memory instead.", exc)

    @staticmethod
    def _key(mode: str, insights_file: Optional[str], question: str, role: str = "employee", extra: str = "") -> str:
        # ``extra`` folds in anything else the answer depends on besides the
        # question itself — currently the live approval-queue snapshot, so a
        # cached "how many requests are pending" doesn't go stale for up to
        # CACHE_TTL_SECONDS while the queue actually changes underneath it.
        digest = hashlib.sha256(f"{role}:{extra}:{question.strip().lower()}".encode("utf-8")).hexdigest()
        return f"chatcache:{mode}:{insights_file or 'none'}:{digest}"

    def get(self, mode: str, insights_file: Optional[str], question: str, role: str = "employee", extra: str = "") -> Optional[dict]:
        key = self._key(mode, insights_file, question, role, extra)
        if self._client is not None:
            try:
                raw = self._client.get(key)
                return json.loads(raw) if raw else None
            except Exception as exc:
                self._warn_once(exc)
        with self._lock:
            entry = self._fallback.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if expires_at < time.time():
                del self._fallback[key]
                return None
            return value

    def set(self, mode: str, insights_file: Optional[str], question: str, value: dict, role: str = "employee", extra: str = "") -> None:
        key = self._key(mode, insights_file, question, role, extra)
        if self._client is not None:
            try:
                self._client.setex(key, CACHE_TTL_SECONDS, json.dumps(value, default=str))
                return
            except Exception as exc:
                self._warn_once(exc)
        with self._lock:
            self._fallback[key] = (time.time() + CACHE_TTL_SECONDS, value)

    def _warn_once(self, exc: Exception) -> None:
        if not self._warned:
            log.warning("Redis call failed (%s); falling back to in-process cache for this process.", exc)
            self._warned = True
        self._client = None


class ChatHistoryStore:
    """Durable log of every question asked of the widget.

    Thin wrapper around ``PostgresStore``: one row per turn in its shared
    ``agent_responses`` table (``source='chat'``), alongside every other
    agent response this app produces — kept separate from the JSON
    action/decision stores since this is an append-only audit log, not
    mutable state.
    """

    SOURCE = "chat"

    def __init__(self, store: PostgresStore):
        self._store = store

    def record(
        self,
        session_id: str,
        mode: str,
        question: str,
        answer: str,
        insights_file: Optional[str],
        citations: list[str],
    ) -> None:
        self._store.record(
            source=self.SOURCE,
            session_id=session_id,
            request={"mode": mode, "question": question, "insights_file": insights_file},
            response={"answer": answer, "citations": citations},
        )

    def recent(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = self._store.recent(session_id=session_id, source=self.SOURCE, limit=limit)
        out = []
        for row in rows:
            request, response = row.get("request") or {}, row.get("response") or {}
            out.append({
                "id": row.get("id"),
                "ts": row.get("created_at"),
                "session_id": row.get("session_id"),
                "mode": request.get("mode"),
                "question": request.get("question"),
                "answer": response.get("answer"),
                "insights_file": request.get("insights_file"),
                "citations": response.get("citations"),
            })
        return out


class ChatContextCache:
    """Redis-backed rolling window of the last few turns per session, so a
    follow-up question ("what about last month?") can be answered with the
    prior turn in view — without a Postgres round trip on every question.

    Deliberately short-lived and lossy: it falls back to "no context" (never
    an error) when Redis is unreachable, and empties whenever Redis restarts
    (see docker-compose.yml's `redis` service — persistence is off on
    purpose) or after ``CONTEXT_TTL_SECONDS`` of inactivity. The permanent
    record of every turn lives in Postgres via ``ChatHistoryStore``.
    """

    def __init__(self, redis_url: str):
        self._client = None
        self._warned = False
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
            client.ping()
            self._client = client
            log.info("Chat context cache using Redis at %s", redis_url)
        except Exception as exc:
            log.warning("Redis unavailable (%s); chat follow-ups will have no conversation context.", exc)

    @staticmethod
    def _key(session_id: str) -> str:
        return f"chatctx:{session_id}"

    def add_turn(self, session_id: str, question: str, answer: str, scope: str = "") -> None:
        """``scope`` should be whatever the answer was actually grounded in —
        typically the insights file path (see ``answer_question``). Turns are
        filtered by scope in ``recent()`` so that switching to a new pipeline
        run (a new insights file) doesn't leak an old run's cost data back
        into a fresh answer just because it's still sitting in this window —
        a real bug hit in practice: a stale answer from an earlier run kept
        reappearing in later replies about a completely different run."""
        if self._client is None:
            return
        key = self._key(session_id)
        try:
            pipe = self._client.pipeline()
            pipe.rpush(key, json.dumps(
                {"question": question, "answer": answer, "scope": scope, "ts": time.time()}, default=str
            ))
            pipe.ltrim(key, -CONTEXT_MAX_TURNS, -1)
            pipe.expire(key, CONTEXT_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            self._warn_once(exc)

    def recent(self, session_id: str, scope: str = "", limit: int = CONTEXT_MAX_TURNS) -> list[dict]:
        """Only turns matching ``scope`` (see ``add_turn``) are returned —
        an empty ``scope`` matches everything, for callers that don't have a
        meaningful scope to filter by."""
        if self._client is None:
            return []
        try:
            raw = self._client.lrange(self._key(session_id), -CONTEXT_MAX_TURNS, -1)
            turns = [json.loads(item) for item in raw]
        except Exception as exc:
            self._warn_once(exc)
            return []
        if scope:
            turns = [t for t in turns if t.get("scope") == scope]
        return turns[-limit:]

    def _warn_once(self, exc: Exception) -> None:
        if not self._warned:
            log.warning("Redis call failed (%s); chat context cache disabled for this process.", exc)
            self._warned = True
        self._client = None
