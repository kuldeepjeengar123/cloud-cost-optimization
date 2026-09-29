"""Semantic cache - real Redis + RediSearch vector index (redis-stack),
replacing the old flat-file JSON stand-in. Embeds each incoming query and
reuses a previous result when a new one is semantically close enough,
instead of calling the LLM again.

Uses Redis's own vector similarity search (FT.CREATE + FT.SEARCH ... KNN),
per the M3 Week 2 spec ("store in Redis vector index using redis-py with
vector search") - not a Python-side brute-force scan. Requires the
redis-stack image (RediSearch module); plain redis:7-alpine has no FT.*
commands. See docker-compose.yml at the repo root.

Same public interface as before (get/set) - pipeline.py and
intent_pipeline.py's calls to SemanticCache(...) only drop the now-irrelevant
persist_path argument (Redis is the persistence layer now); embeddings and
similarity_threshold are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any, Optional, Tuple

import redis
from redis.commands.search.field import TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

_INDEX_NAME = "semantic_cache_idx"
_KEY_PREFIX = "semcache:"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _to_bytes(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _key_for(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


class SemanticCache:
    def __init__(
        self,
        embeddings,
        similarity_threshold: float = 0.98,
        redis_url: Optional[str] = None,
    ):
        self._embeddings = embeddings
        self._threshold = similarity_threshold
        redis_url = redis_url or os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
        self._client = redis.from_url(redis_url, decode_responses=False)
        self._index_ready = self._index_exists()

    def _index_exists(self) -> bool:
        try:
            self._client.ft(_INDEX_NAME).info()
            return True
        except redis.exceptions.ResponseError:
            return False

    def _create_index(self, dim: int) -> None:
        """Deferred until the first .set() call, since the embedding
        dimension depends on whichever model providers.build_embeddings_client
        is actually configured with - never hardcoded here."""
        schema = (
            TextField("text"),
            TextField("value_json"),
            VectorField(
                "embedding",
                "FLAT",
                {"TYPE": "FLOAT32", "DIM": dim, "DISTANCE_METRIC": "COSINE"},
            ),
        )
        self._client.ft(_INDEX_NAME).create_index(
            schema,
            definition=IndexDefinition(prefix=[_KEY_PREFIX], index_type=IndexType.HASH),
        )
        self._index_ready = True

    def get(self, text: str) -> Tuple[Optional[Any], bool]:
        """Return (cached_value, True) on a hit, or (None, False) on a miss."""
        if not self._index_ready:
            return None, False

        query_vec = self._embeddings.embed_query(text)
        query = (
            Query("*=>[KNN 1 @embedding $vec AS score]")
            .sort_by("score")
            .return_fields("value_json", "score")
            .dialect(2)
        )
        results = self._client.ft(_INDEX_NAME).search(
            query, query_params={"vec": _to_bytes(query_vec)}
        )
        if not results.docs:
            return None, False

        doc = results.docs[0]
        # RediSearch COSINE "score" here is a distance (0 = identical, 2 =
        # opposite), not a similarity - convert so the threshold means the
        # same thing it always has (1.0 = identical, matching the old
        # cosine-similarity semantics).
        distance = float(doc.score)
        similarity = 1 - distance / 2
        if similarity >= self._threshold:
            return json.loads(doc.value_json), True
        return None, False

    def set(self, text: str, value: Any) -> None:
        vec = self._embeddings.embed_query(text)
        if not self._index_ready:
            self._create_index(len(vec))

        self._client.hset(
            _key_for(text),
            mapping={
                "text": text,
                "value_json": json.dumps(value, default=str),
                "embedding": _to_bytes(vec),
            },
        )
