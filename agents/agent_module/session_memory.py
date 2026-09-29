"""Session memory - real Redis-backed conversation history with TTL.

M3 docx Week 1: "Build a session memory store (store/retrieve conversation
turns by session ID with TTL)." Pure Redis list + EXPIRE - no embeddings, no
LLM call needed to exercise this at all.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import redis

_KEY_PREFIX = "session:"
DEFAULT_TTL_SECONDS = 3600  # 1 hour
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class SessionMemory:
    def __init__(self, redis_url: str | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        redis_url = redis_url or os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    def add_turn(self, session_id: str, role: str, content: str) -> None:
        key = self._key(session_id)
        turn = json.dumps({"role": role, "content": content})
        self._client.rpush(key, turn)
        self._client.expire(key, self._ttl)

    def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        raw_turns = self._client.lrange(self._key(session_id), 0, -1)
        return [json.loads(t) for t in raw_turns]

    def ttl_remaining(self, session_id: str) -> int:
        """Seconds until this session expires; -2 if it doesn't exist, -1 if it has no TTL set."""
        return self._client.ttl(self._key(session_id))

    def clear(self, session_id: str) -> None:
        self._client.delete(self._key(session_id))
