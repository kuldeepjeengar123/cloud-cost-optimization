"""Step 4 — Combine All Responses into a single structured payload.

Also resolves the chart specs from Step 3.1 into actual ``data`` arrays so the
frontend doesn't have to do any data wrangling — it just plots ``data``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..utils.logger import get_logger

log = get_logger("pipeline.step4")

MAX_POINTS = 25


def _resolve_chart_data(spec: dict, context: dict) -> list[dict]:
    """Return [{x, y}, ...] for a chart spec, picking the best source table.

    Looks up the LLM-supplied ``data_table`` in correlations first, then in raw
    records, then falls back to a fuzzy match. Aggregates duplicate x values by
    summing y. Time-series x columns stay chronological; everything else is
    sorted by y descending and truncated to ``MAX_POINTS``.
    """
    x_field = (spec.get("x") or "").strip().lower()
    y_field = (spec.get("y") or "cost").strip().lower()
    table_name = (spec.get("data_table") or "").strip()
    if not x_field or not table_name:
        return []

    correlations = context.get("correlations", {}) or {}
    records = context.get("records", {}) or {}

    rows: list[dict] = []
    if table_name in correlations:
        rows = correlations[table_name]
    elif table_name in records:
        rows = records[table_name]
    else:
        # fuzzy match: case-insensitive substring against both maps
        candidates = list(correlations.items()) + list(records.items())
        match = next(
            (data for name, data in candidates if table_name.lower() in name.lower()),
            None,
        )
        rows = match or []

    if not rows:
        return []

    buckets: dict[str, float] = defaultdict(float)
    chronological_keys: list[str] = []
    seen_keys: set[str] = set()
    for row in rows:
        x_val = row.get(x_field)
        y_val = row.get(y_field)
        if x_val is None or not isinstance(y_val, (int, float)):
            continue
        key = str(x_val)
        if key not in seen_keys:
            chronological_keys.append(key)
            seen_keys.add(key)
        buckets[key] += float(y_val)

    if not buckets:
        return []

    is_time_series = x_field in {"date", "day", "timestamp", "month"}
    if is_time_series:
        pairs = [(k, buckets[k]) for k in chronological_keys]
        pairs.sort(key=lambda p: p[0])  # ISO date strings sort correctly
    else:
        pairs = sorted(buckets.items(), key=lambda p: p[1], reverse=True)[:MAX_POINTS]

    return [{"x": k, "y": round(v, 6)} for k, v in pairs]


def run_step4_combine(
    context: dict,
    charts: dict,
    analysis: dict,
    summary: dict,
) -> dict:
    log.info("Step 4: combine visual + analytical + summary")

    resolved_charts = []
    for spec in charts.get("charts", []):
        data = _resolve_chart_data(spec, context)
        resolved = {**spec, "data": data, "x_field": spec.get("x"), "y_field": spec.get("y")}
        if not data:
            log.warning("Chart '%s' had no resolvable data (table=%s)",
                        spec.get("id") or spec.get("title"), spec.get("data_table"))
        resolved_charts.append(resolved)

    return {
        "schema_map": context.get("schema_map", {}),
        "business_metadata": context.get("business_metadata", {}),
        "correlations": context.get("correlations", {}),
        "charts": resolved_charts,
        "analysis": {
            "kpis": analysis.get("kpis", {}),
            "anomalies": analysis.get("anomalies", []),
            "trends": analysis.get("trends", []),
            "benchmarks": analysis.get("benchmarks", []),
            "forecast": analysis.get("forecast", {}),
            "tag_findings": analysis.get("tag_findings", []),
        },
        "summary": summary,
    }
