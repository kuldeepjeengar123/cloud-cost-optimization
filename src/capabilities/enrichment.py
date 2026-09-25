"""Enrichment — add derived fields like cost tier and date components.

Cheap, deterministic enrichments only; the LLM steps handle interpretation.
"""

from __future__ import annotations

from ..utils.logger import get_logger

log = get_logger("capabilities.enrichment")


def _tier(cost: float) -> str:
    if cost <= 0:
        return "zero"
    if cost < 0.001:
        return "negligible"
    if cost < 0.01:
        return "low"
    if cost < 0.1:
        return "medium"
    if cost < 1:
        return "high"
    return "very_high"


def enrich_records(records: dict[str, list[dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table, rows in records.items():
        enriched = []
        for row in rows:
            new = dict(row)
            cost_val = row.get("cost")
            if isinstance(cost_val, (int, float)):
                new["cost_tier"] = _tier(float(cost_val))
            date_val = row.get("date")
            if isinstance(date_val, str) and len(date_val) >= 10:
                new["year"] = date_val[:4]
                new["month"] = date_val[5:7]
                new["day"] = date_val[8:10]
            enriched.append(new)
        out[table] = enriched
        log.info("enrich: added derived fields to %s rows in %s", len(enriched), table)
    return out
