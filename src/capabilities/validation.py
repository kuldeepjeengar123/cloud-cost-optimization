"""Data validation — schema integrity and basic type checks."""

from __future__ import annotations

from ..utils.logger import get_logger

log = get_logger("capabilities.validation")


def validate_records(records: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drop empty rows and rows missing all expected fields. Logs counts."""

    cleaned: dict[str, list[dict]] = {}
    for table, rows in records.items():
        kept = [r for r in rows if r and any(v not in (None, "") for v in r.values())]
        dropped = len(rows) - len(kept)
        if dropped:
            log.info("validate: dropped %s empty rows from %s", dropped, table)
        cleaned[table] = kept
    return cleaned
