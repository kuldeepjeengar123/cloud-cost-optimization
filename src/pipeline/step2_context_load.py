"""Step 2 — Context Load.

Applies enrichment, builds relationships/correlations across tables, computes a
compact summary suitable as LLM context, and exposes the full normalized
records for downstream steps. Enrichment/correlations are deterministic, but
anomaly detection here is LLM-first (see ``_llm_detect_anomalies``): the LLM
judges what counts as a spike from the same per-dimension daily series the
deterministic ``detect_anomalies`` (see capabilities/anomaly_detection.py)
would otherwise apply a fixed z-score threshold to. That detector only runs
as a fallback, when the LLM call fails or returns nothing usable — never
raises, same "always say something" contract every LLM step here follows.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from ..capabilities import detect_anomalies, enrich_records
from ..config import PipelineConfig
from ..llm.client import LLMClient
from ..prompts import load_prompt
from ..utils.logger import get_logger

log = get_logger("pipeline.step2")

SYSTEM_PROMPT = load_prompt("detect_anomalies")

# Same dimension columns detect_anomalies() groups by — one per table (the
# first one present), so each series is "this table's cost for one dimension
# value, over time" rather than the whole table's total.
_DIMENSION_COLUMNS = ("service", "region", "instance_type", "project_tag")
# Caps keep the prompt bounded on a large account: highest-cost series first,
# most recent days first — the same "top N" tradeoff every other prompt in
# this pipeline already makes (see step1_normalize.py's sample rows, etc.).
_MAX_SERIES_PER_TABLE = 15
_MAX_POINTS_PER_SERIES = 60


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


def _anomaly_series_snapshot(records: dict[str, list[dict]]) -> dict:
    """Per table: which dimension column it has (if any), and each dimension
    value's chronological ``[date, cost]`` series — plain data for the LLM to
    judge, not a precomputed verdict (contrast with ``correlations`` above,
    which sums away the per-date detail this needs)."""
    snapshot: dict[str, dict] = {}
    for table, rows in (records or {}).items():
        if not rows or "cost" not in rows[0] or "date" not in rows[0]:
            continue
        dim = next((d for d in _DIMENSION_COLUMNS if d in rows[0]), None)

        totals: dict[str, float] = defaultdict(float)
        series: dict[str, list[list]] = defaultdict(list)
        for row in rows:
            cost, date = row.get("cost"), row.get("date")
            if not isinstance(cost, (int, float)) or not date:
                continue
            key = str(row.get(dim)) if dim else table
            totals[key] += float(cost)
            series[key].append([str(date), round(float(cost), 6)])

        top_keys = sorted(totals, key=lambda k: totals[k], reverse=True)[:_MAX_SERIES_PER_TABLE]
        snapshot[table] = {
            "dimension": dim,
            "series": {k: sorted(series[k])[-_MAX_POINTS_PER_SERIES:] for k in top_keys},
        }
    return snapshot


def _llm_detect_anomalies(records: dict[str, list[dict]], llm: LLMClient, cfg: PipelineConfig) -> list[dict]:
    """LLM-first anomaly search — raises on a failed/unusable call so the
    caller can fall back to the deterministic detector; a clean "nothing
    anomalous" verdict (an empty list) is a legitimate result, not a failure."""
    snapshot = _anomaly_series_snapshot(records)
    if not snapshot:
        return []
    user_prompt = (
        "Per-table daily cost series, by dimension value (untrusted data):\n"
        + json.dumps(snapshot, default=str, indent=2)
    )
    raw = llm.complete(system=SYSTEM_PROMPT, user=user_prompt, model=cfg.llm.model_analysis, max_tokens=1800)
    parsed = LLMClient.extract_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("anomalies"), list):
        raise ValueError("LLM anomaly detection returned no usable JSON")

    findings: list[dict] = []
    for item in parsed["anomalies"]:
        if not isinstance(item, dict) or not item.get("finding"):
            continue
        severity = item.get("severity")
        findings.append({
            "finding": str(item["finding"]),
            "severity": severity if severity in ("low", "medium", "high") else "medium",
            "evidence": str(item.get("evidence") or ""),
            "source": "llm",
            "table": item.get("table"),
            "dimension": item.get("dimension"),
            "date": item.get("date"),
        })
    return findings[:20]


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


def run_step2_context_load(cfg: PipelineConfig, step1_output: dict, llm: LLMClient) -> dict[str, Any]:
    log.info("Step 2: context load (enrichment + correlations)")

    records: dict[str, list[dict]] = step1_output["records"]
    if cfg.capabilities.enrich:
        records = enrich_records(records)

    # Root-cause correlation (explain_anomalies), forecasting and tag-governance
    # checks run as their own orchestrator steps right after this one — see
    # orchestrator.py — so each shows up as its own node in the pipeline
    # flowchart instead of being invisibly folded into this step.
    anomaly_signals: list[dict] = []
    if cfg.capabilities.detect_anomalies:
        try:
            anomaly_signals = _llm_detect_anomalies(records, llm, cfg)
        except Exception as exc:
            log.warning("LLM anomaly detection failed (%s); using the deterministic detector.", exc)
            anomaly_signals = detect_anomalies(records)

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
