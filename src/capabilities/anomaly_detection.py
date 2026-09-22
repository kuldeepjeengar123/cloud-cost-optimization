"""Deterministic anomaly detection over normalized cost records.

Sits alongside the other capabilities/ passes (validate, dedup, normalize,
enrich) with the same calling convention: takes the normalized
``dict[str, list[dict]]`` records and returns plain data — here, a list of
anomaly-shaped findings rather than transformed records. Step 2 hands these
to Step 3.2 as an additional signal the LLM's own anomaly search doesn't
depend on (see ``step2_context_load`` / ``step3_2_analysis``), so a spike
still surfaces even if the LLM misses it or the LLM call fails outright.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev

_DIMENSION_COLUMNS = ("service", "region", "instance_type", "project_tag")
_Z_THRESHOLD = 2.0
_MIN_POINTS = 4  # a series shorter than this doesn't have a meaningful baseline


def detect_anomalies(records: dict[str, list[dict]]) -> list[dict]:
    """Flag cost-per-day spikes per (table, dimension value).

    A day is flagged when its cost is more than ``_Z_THRESHOLD`` standard
    deviations above that series' mean, or — for series too short/flat for a
    stdev to mean anything — more than 75% above the mean.
    """
    findings: list[dict] = []
    for table, rows in (records or {}).items():
        if not rows or "cost" not in rows[0] or "date" not in rows[0]:
            continue
        dim = next((d for d in _DIMENSION_COLUMNS if d in rows[0]), None)

        series: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for row in rows:
            cost, date = row.get("cost"), row.get("date")
            if not isinstance(cost, (int, float)) or not date:
                continue
            key = str(row.get(dim)) if dim else table
            series[key].append((str(date), float(cost)))

        for key, points in series.items():
            points.sort()
            costs = [c for _, c in points]
            if len(costs) < _MIN_POINTS:
                continue
            avg = mean(costs)
            if avg <= 0:
                continue
            spread = pstdev(costs)
            for date, cost in points:
                spike = (cost - avg) / spread if spread > 0 else (cost - avg) / avg
                threshold = _Z_THRESHOLD if spread > 0 else 0.75
                if spike <= threshold:
                    continue
                pct = round((cost / avg - 1) * 100)
                findings.append({
                    "finding": f"Cost spike in {table}" + (f" for {dim}={key}" if dim else "") + f" on {date}",
                    "severity": "high" if pct >= 100 else "medium",
                    "evidence": f"${cost:.2f} on {date} vs. an average of ${avg:.2f} "
                                f"({pct:+d}%) across {len(costs)} days.",
                    "source": "detector",
                    # Structured fields (additive — existing consumers only read
                    # finding/severity/evidence) so capabilities.root_cause can
                    # correlate this spike against other dimensions on the same
                    # table/date without re-parsing the finding text.
                    "table": table,
                    "dimension": dim,
                    "date": date,
                    "cost": round(cost, 6),
                    "avg": round(avg, 6),
                })

    findings.sort(key=lambda f: f["severity"] != "high")
    return findings[:20]
