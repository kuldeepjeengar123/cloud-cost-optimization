"""Best-effort explanation for *why* a detected cost spike happened.

Takes the structured findings from ``anomaly_detection.detect_anomalies``
(each carries the table/date it fired on, plus which dimension column was
flagged) and looks at every *other* dimension breakdown of that same table on
that same date, ranking whichever value grew the most above its own normal
that day. This is a correlation, not a causal proof — worded as "coincided
with", never "was caused by" — but it turns "cost went up" into a concrete
lead an engineer can go check, instead of just a date and a dollar amount.
"""

from __future__ import annotations

from collections import defaultdict

_OTHER_DIMENSIONS = ("service", "region", "instance_type", "project_tag")
_MAX_DRIVERS = 3
_MIN_HISTORY = 2  # need at least this many days for a value's "own average" to mean anything
_DRIVER_PCT_THRESHOLD = 30  # how far above its own average counts as a lead


def _dimension_day_totals(rows: list[dict], dimension: str) -> dict[tuple[str, str], float]:
    """(date, dimension_value) -> summed cost, for one table."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        cost, date, value = row.get("cost"), row.get("date"), row.get(dimension)
        if isinstance(cost, (int, float)) and date and value is not None:
            totals[(str(date), str(value))] += float(cost)
    return totals


def _drivers_for(rows: list[dict], date: str, flagged_dim: str | None) -> list[dict]:
    drivers = []
    for dim in _OTHER_DIMENSIONS:
        if dim == flagged_dim or not rows or dim not in rows[0]:
            continue
        totals = _dimension_day_totals(rows, dim)
        by_value: dict[str, list[float]] = defaultdict(list)
        for (d, v), c in totals.items():
            by_value[v].append(c)
        for value, cost_that_day in ((v, c) for (d, v), c in totals.items() if d == date):
            history = by_value[value]
            if len(history) < _MIN_HISTORY:
                continue
            avg = sum(history) / len(history)
            if avg <= 0:
                continue
            pct = round((cost_that_day / avg - 1) * 100)
            if pct > _DRIVER_PCT_THRESHOLD:
                drivers.append({"dimension": dim, "value": value, "pct_above_own_avg": pct})
    drivers.sort(key=lambda d: d["pct_above_own_avg"], reverse=True)
    return drivers[:_MAX_DRIVERS]


def explain_anomalies(records: dict[str, list[dict]], anomaly_signals: list[dict]) -> list[dict]:
    """Attach a ``drivers`` list to each finding that has enough structure to
    correlate (a ``table`` and ``date``, as added by ``detect_anomalies``);
    findings from another source (e.g. the LLM's own anomaly search, merged in
    later) are passed through untouched."""
    explained: list[dict] = []
    for finding in anomaly_signals:
        table, date = finding.get("table"), finding.get("date")
        rows = (records or {}).get(table) or []
        if not table or not date or not rows:
            explained.append(finding)
            continue

        drivers = _drivers_for(rows, date, finding.get("dimension"))
        if not drivers:
            explained.append(finding)
            continue

        updated = dict(finding)
        updated["drivers"] = drivers
        lead = ", ".join(f"{d['value']} ({d['dimension']}, +{d['pct_above_own_avg']}%)" for d in drivers)
        updated["evidence"] = f"{finding.get('evidence', '')} Coincided with a jump in: {lead}."
        explained.append(updated)
    return explained
