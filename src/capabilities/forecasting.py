"""Deterministic cost-trend forecasting over normalized cost records.

Same calling convention as anomaly_detection.py: a pure function of the
normalized records that returns plain data. Step 2 hands this to Step 3.2 (and,
via the analysis dict, to Step 3.3's fallback) so a rising spend trend surfaces
as a recommendation *before* it becomes a budget overrun, not after.
"""

from __future__ import annotations

from collections import defaultdict

_MIN_POINTS = 6  # fewer days than this and a trend split isn't meaningful
_TREND_FLAG_PCT = 15  # recent-half vs. prior-half growth worth flagging


def _daily_totals(records: dict[str, list[dict]]) -> dict[str, float]:
    """Sum cost per date across every table that has date+cost columns."""
    totals: dict[str, float] = defaultdict(float)
    for rows in (records or {}).values():
        if not rows or "cost" not in rows[0] or "date" not in rows[0]:
            continue
        for row in rows:
            cost, date = row.get("cost"), row.get("date")
            if isinstance(cost, (int, float)) and date:
                totals[str(date)] += float(cost)
    return totals


def forecast_costs(records: dict[str, list[dict]]) -> dict:
    """Project the next 30 days of spend from the recent daily trend.

    Splits the observed daily series in half; the recent half's average daily
    run rate vs. the prior half's gives a trend percentage, applied to project
    a 30-day total. Returns ``{}`` (no opinion) when there isn't enough daily
    history to trust a trend — a flat/short series is not worth projecting.
    """
    totals = _daily_totals(records)
    if len(totals) < _MIN_POINTS:
        return {}

    series = sorted(totals.items())  # [(date, cost), ...] chronological
    costs = [c for _, c in series]
    mid = len(costs) // 2
    prior, recent = costs[:mid], costs[mid:]
    prior_avg = sum(prior) / len(prior)
    recent_avg = sum(recent) / len(recent)

    trend_pct = round((recent_avg / prior_avg - 1) * 100) if prior_avg > 0 else 0
    projected_30d = round(recent_avg * 30, 2)

    return {
        "days_observed": len(series),
        "date_range": [series[0][0], series[-1][0]],
        "prior_period_daily_avg": round(prior_avg, 2),
        "recent_period_daily_avg": round(recent_avg, 2),
        "trend_pct": trend_pct,
        "projected_30d_total": projected_30d,
        "flag": trend_pct >= _TREND_FLAG_PCT,
    }
