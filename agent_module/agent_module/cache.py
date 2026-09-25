from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, List, Optional, Tuple

from langchain_openai import OpenAIEmbeddings


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticCache:
    """Local stand-in for a real semantic cache (e.g. Redis plus a vector
    index). Embeds each incoming query and reuses a previous result when a
    new one is semantically close enough, instead of calling the LLM again.

    If persist_path is given, entries are also written to that file and
    reloaded from it on startup, so the cache survives a process restart —
    not just an in-memory list. If omitted, behavior is unchanged: the cache
    lives only for the current process."""

    def __init__(
        self,
        embeddings: OpenAIEmbeddings,
        similarity_threshold: float = 0.98,
        persist_path: Optional[Path] = None,
    ):
        self._embeddings = embeddings
        self._threshold = similarity_threshold
        self._persist_path = persist_path
        self._entries: List[Tuple[List[float], Any]] = []
        if self._persist_path is not None and self._persist_path.exists():
            self._load()

    def _load(self) -> None:
        with self._persist_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self._entries = [(item["vector"], item["value"]) for item in data]

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"vector": vec, "value": value} for vec, value in self._entries]
        with self._persist_path.open("w", encoding="utf-8") as f:
            json.dump(data, f)

    def get(self, text: str) -> Tuple[Optional[Any], bool]:
        """Return (cached_value, True) on a hit, or (None, False) on a miss."""
        if not self._entries:
            return None, False
        query_vec = self._embeddings.embed_query(text)
        best_value = None
        best_score = -1.0
        for vec, value in self._entries:
            score = _cosine_similarity(query_vec, vec)
            if score > best_score:
                best_score = score
                best_value = value
        if best_score >= self._threshold:
            return best_value, True
        return None, False

    def set(self, text: str, value: Any) -> None:
        vec = self._embeddings.embed_query(text)
        self._entries.append((vec, value))
        self._save()
