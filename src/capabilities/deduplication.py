"""Deduplication — drop exact-duplicate rows per table."""

from __future__ import annotations

import json

from ..utils.logger import get_logger

log = get_logger("capabilities.deduplication")


def deduplicate_records(records: dict[str, list[dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table, rows in records.items():
        seen: set[str] = set()
        unique: list[dict] = []
        for row in rows:
            key = json.dumps(row, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        if len(unique) != len(rows):
            log.info("dedup: %s -> %s rows in %s", len(rows), len(unique), table)
        out[table] = unique
    return out
