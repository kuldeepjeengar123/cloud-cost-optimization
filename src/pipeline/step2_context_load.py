"""Step 2 — Context Load.

Applies enrichment, builds relationships/correlations across tables, computes a
compact summary suitable as LLM context, and exposes the full normalized
records for downstream steps. No LLM call here; this is the deterministic
"load enriched context" stage from the diagram.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..capabilities import detect_anomalies, enrich_records
from ..config import PipelineConfig
from ..utils.logger import get_logger

log = get_logger("pipeline.step2")


def _aggregate(
    records: list[dict], group_by: str, value_field: str = "cost", chronological: bool = False
) -> list[dict]:
    """Sum ``value_field`` per distinct ``group_by`` value.

    Sorted by value descending (highest-cost entity first) by default; pass
    ``chronological=True`` for a date-like key so a time series reads in
    order instead of cost-ranked.
    """
    buckets: dict[str, float] = defaultdict(float)
    for row in records:
        key = row.get(group_by)
        val = row.get(value_field)
        if key is None or not isinstance(val, (int, float)):
            continue
        buckets[str(key)] += float(val)
    rows = [{group_by: k, value_field: round(v, 6)} for k, v in buckets.items()]
    if chronological:
        rows.sort(key=lambda r: r[group_by])  # ISO date strings sort correctly
    else:
        rows.sort(key=lambda r: r[value_field], reverse=True)
    return rows


def _build_compact_context(records: dict[str, list[dict]]) -> str:
    parts = ["=== AWS COST CONTEXT (compact) ==="]
    for table, rows in records.items():
        parts.append(f"\n## {table} ({len(rows)} rows)")
        cost_total = sum(
            r.get("cost", 0) for r in rows if isinstance(r.get("cost"), (int, float))
        )
        if cost_total:
            parts.append(f"  total_cost: ${cost_total:.6f}")
        if rows:
            parts.append(f"  sample: {rows[0]}")
    return "\n".join(parts)


def run_step2_context_load(cfg: PipelineConfig, step1_output: dict) -> dict[str, Any]:
    log.info("Step 2: context load (enrichment + correlations)")

    records: dict[str, list[dict]] = step1_output["records"]
    if cfg.capabilities.enrich:
        records = enrich_records(records)

    # Root-cause correlation (explain_anomalies), forecasting and tag-governance
    # checks run as their own orchestrator steps right after this one — see
    # orchestrator.py — so each shows up as its own node in the pipeline
    # flowchart instead of being invisibly folded into this step.
    anomaly_signals = detect_anomalies(records) if cfg.capabilities.detect_anomalies else []

    correlations: dict[str, Any] = {}
    for table, rows in records.items():
        if not rows:
            continue
        if "service" in rows[0]:
            correlations[f"{table}__by_service"] = _aggregate(rows, "service")
        if "region" in rows[0]:
            correlations[f"{table}__by_region"] = _aggregate(rows, "region")
        if "date" in rows[0]:
            correlations[f"{table}__by_date"] = _aggregate(rows, "date", chronological=True)
        if "instance_type" in rows[0]:
            correlations[f"{table}__by_instance_type"] = _aggregate(rows, "instance_type")
        if "project_tag" in rows[0]:
            correlations[f"{table}__by_project_tag"] = _aggregate(rows, "project_tag")

    total_cost = 0.0
    for rows in records.values():
        for row in rows:
            if isinstance(row.get("cost"), (int, float)):
                total_cost += float(row["cost"])

    business_metadata = {
        "total_cost_observed": round(total_cost, 6),
        "tables": {t: len(rows) for t, rows in records.items()},
    }

    return {
        "records": records,
        "schema_map": step1_output.get("schema_map", {}),
        "sources": step1_output.get("sources", []),
        "correlations": correlations,
        "business_metadata": business_metadata,
        "compact_context": _build_compact_context(records),
        "anomaly_signals": anomaly_signals,
    }
