"""Normalization — standardize column names, casing, and numeric/date formats.

Keeps original values around in ``_raw`` only if a conversion happens, so the
downstream LLM steps see clean types but anomalies can still be traced.
"""

from __future__ import annotations

from datetime import datetime

from ..utils.logger import get_logger

log = get_logger("capabilities.normalization")

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y")


def _norm_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _try_date(value):
    if not isinstance(value, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_records(records: dict[str, list[dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table, rows in records.items():
        normalized: list[dict] = []
        for row in rows:
            new_row: dict = {}
            for key, value in row.items():
                norm_key = _norm_key(key)
                if isinstance(value, str):
                    iso = _try_date(value)
                    if iso:
                        new_row[norm_key] = iso
                        continue
                if isinstance(value, (int, float)):
                    new_row[norm_key] = round(float(value), 6)
                else:
                    new_row[norm_key] = value
            normalized.append(new_row)
        out[table] = normalized
        log.info("normalize: %s rows in %s", len(normalized), table)
    return out
