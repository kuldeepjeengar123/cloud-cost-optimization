"""Chat widget persistence: a Redis answer cache and a SQLite question log.

Both are best-effort. Redis is a nice-to-have (cuts repeat-question latency
and LLM spend); SQLite is the durable record of what users asked. Neither
failure mode should ever surface as a broken chat reply — matching how the
rest of the action API never 500s a user-facing button.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger

log = get_logger("chat.memory")

CACHE_TTL_SECONDS = 3600


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
    """Durable local-file log of every question asked of the widget.

    One row per turn, written to a SQLite file under the pipeline's output
    folder — kept separate from the JSON action/decision stores since this is
    an append-only audit log, not mutable state.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    insights_file TEXT,
                    citations TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(
        self,
        session_id: str,
        mode: str,
        question: str,
        answer: str,
        insights_file: Optional[str],
        citations: list[str],
    ) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO chat_messages (ts, session_id, mode, question, answer, insights_file, citations) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), session_id, mode, question, answer, insights_file, json.dumps(citations)),
                )
        except sqlite3.Error as exc:
            log.warning("Could not write chat history (%s)", exc)

    def recent(self, session_id: str, limit: int = 20) -> list[dict]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            return [dict(row) for row in rows][::-1]
        except sqlite3.Error as exc:
            log.warning("Could not read chat history (%s)", exc)
            return []
