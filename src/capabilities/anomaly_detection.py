"""Anomaly detection over normalized cost records - two independent signals.

Sits alongside the other capabilities/ passes (validate, dedup, normalize,
enrich) with the same calling convention: takes the normalized
``dict[str, list[dict]]`` records and returns plain data — here, a list of
anomaly-shaped findings rather than transformed records. Step 2 hands these
to Step 3.2 as an additional signal the LLM's own anomaly search doesn't
depend on (see ``step2_context_load`` / ``step3_2_analysis``), so a spike
still surfaces even if the LLM misses it or the LLM call fails outright.

Two detectors, both deterministic (no LLM):

1. Z-score per series (``_detect_anomalies_zscore``, unchanged from before) -
   flags a single (table, dimension value)'s day as a spike relative to its
   own history. Fast, always available, exactly the same output shape as
   before this upgrade.

2. Isolation Forest across the whole daily cost picture
   (``_isolation_forest_daily_findings``, new) - per the M3 Week 5 spec
   ("multi-dimensional anomalies across service x region x time using
   scikit-learn"). The z-score detector only ever looks at one series in
   isolation; this one builds a single feature matrix - one row per date,
   one column per (table, dimension value) pair actually observed across
   every input table - and flags whole DAYS whose overall pattern across
   every dimension simultaneously is unusual, which a single-series check
   can't see (e.g. many small, individually-unremarkable increases that are
   jointly unusual). Requires scikit-learn; degrades to "no extra findings"
   if it isn't installed, or if there isn't enough data/columns for the
   model to be meaningful - never raises, per this module's existing
   fault-tolerance contract (see orchestrator.run_forecast_agent and
   friends).
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev

_DIMENSION_COLUMNS = ("service", "region", "instance_type", "project_tag", "cost_centre", "environment")
_Z_THRESHOLD = 2.0
_MIN_POINTS = 4  # a series shorter than this doesn't have a meaningful baseline

_IF_MIN_DATES = 10   # fewer distinct dates than this and IsolationForest has no meaningful baseline
_IF_MIN_FEATURES = 2  # need at least 2 dimension-value columns for "multi-dimensional" to mean anything
_IF_SCORE_HIGH = -0.1  # decision_function scores below this are treated as high severity (heuristic)


def _detect_anomalies_zscore(records: dict[str, list[dict]]) -> list[dict]:
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
                    "method": "zscore",
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

    return findings


def _build_daily_feature_matrix(records: dict[str, list[dict]]):
    """One row per date, one column per (table, dimension_value) pair
    actually observed. Returns (dates_sorted, feature_names, matrix) where
    matrix is a plain list-of-lists (no numpy dependency at this stage —
    only the caller, which already requires scikit-learn/numpy, converts
    it). A (table, value) combination with no row on a given date gets 0.0
    for that date, not a missing value."""
    # (table, dimension_value) -> {date: cost}
    per_column: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    all_dates: set[str] = set()

    for table, rows in (records or {}).items():
        if not rows or "cost" not in rows[0] or "date" not in rows[0]:
            continue
        dim = next((d for d in _DIMENSION_COLUMNS if d in rows[0]), None)
        for row in rows:
            cost, date = row.get("cost"), row.get("date")
            if not isinstance(cost, (int, float)) or not date:
                continue
            date = str(date)
            all_dates.add(date)
            value = str(row.get(dim)) if dim else table
            per_column[(table, value)][date] += float(cost)

    dates_sorted = sorted(all_dates)
    feature_names = [f"{table}:{value}" for (table, value) in sorted(per_column)]
    matrix = [
        [per_column[(table, value)].get(date, 0.0) for (table, value) in sorted(per_column)]
        for date in dates_sorted
    ]
    return dates_sorted, feature_names, matrix


def _isolation_forest_daily_findings(records: dict[str, list[dict]]) -> list[dict]:
    dates, feature_names, matrix = _build_daily_feature_matrix(records)
    if len(dates) < _IF_MIN_DATES or len(feature_names) < _IF_MIN_FEATURES:
        return []

    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return []  # scikit-learn/numpy not installed - z-score signal still stands alone

    arr = np.asarray(matrix, dtype=float)
    model = IsolationForest(contamination="auto", random_state=0)
    labels = model.fit_predict(arr)
    scores = model.decision_function(arr)

    col_means = arr.mean(axis=0)
    col_stds = arr.std(axis=0)

    findings: list[dict] = []
    for i, date in enumerate(dates):
        if labels[i] != -1:
            continue
        row = arr[i]
        deviations = []
        for j, name in enumerate(feature_names):
            z = (row[j] - col_means[j]) / col_stds[j] if col_stds[j] > 0 else 0.0
            deviations.append((name, float(row[j]), float(col_means[j]), z))
        deviations.sort(key=lambda d: abs(d[3]), reverse=True)
        top = deviations[:3]
        lead = ", ".join(f"{name} (${val:.2f} vs. avg ${avg:.2f})" for name, val, avg, _z in top if val > 0 or avg > 0)

        score = float(scores[i])
        findings.append({
            "finding": f"Unusual overall cost pattern on {date}",
            "severity": "high" if score < _IF_SCORE_HIGH else "medium",
            "evidence": f"Isolation Forest flagged this day as anomalous across {len(feature_names)} "
                        f"cost dimensions (service x region x tag x instance type, jointly)."
                        + (f" Largest deviations: {lead}." if lead else ""),
            "source": "detector",
            "method": "isolation_forest",
            "date": date,
            "isolation_score": round(score, 4),
        })

    return findings


def detect_anomalies(records: dict[str, list[dict]], *, include_isolation_forest: bool = True) -> list[dict]:
    """Combine both detectors' findings, high-severity first. The Isolation
    Forest pass is wrapped in a broad except so a scikit-learn/numpy issue
    (missing dependency, bad input shape) can never take down the z-score
    signal, which has run standalone since before this upgrade and must keep
    doing so.

    ``include_isolation_forest`` defaults to True (matches calling this
    function directly, e.g. in tests) - step2_context_load.py passes
    ``cfg.capabilities.detect_anomalies_isolation_forest`` instead, so it can
    be turned off per-run/per-test without touching this default."""
    findings = _detect_anomalies_zscore(records)

    if include_isolation_forest:
        try:
            findings.extend(_isolation_forest_daily_findings(records))
        except Exception:
            pass

    findings.sort(key=lambda f: f["severity"] != "high")
    return findings[:20]
