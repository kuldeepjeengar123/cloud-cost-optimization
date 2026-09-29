"""Cost-trend forecasting over normalized cost records - a deterministic
trend-split base, extended with Prophet-based 30/60/90-day projections and
what-if scenario evaluation, per the M3 Week 6 spec.

Same calling convention as anomaly_detection.py: pure functions of the
normalized records that return plain data. Step 2 hands ``forecast_costs``'s
result to Step 3.2 (and, via the analysis dict, to Step 3.3's fallback) so a
rising spend trend surfaces as a recommendation *before* it becomes a budget
overrun, not after.

``forecast_costs``'s original keys (days_observed, date_range,
prior_period_daily_avg, recent_period_daily_avg, trend_pct,
projected_30d_total, flag) and its ``{}``-on-insufficient-data behavior are
unchanged from before this upgrade - existing callers/tests keep working
unmodified. Prophet adds a new, additive ``prophet_forecasts`` key when
there's enough history and the ``prophet`` package is installed; its absence
(not installed, or too little history) is not an error - the trend-split
result stands alone, same as always.
"""

from __future__ import annotations

from collections import defaultdict

_MIN_POINTS = 6  # fewer days than this and a trend split isn't meaningful
_TREND_FLAG_PCT = 15  # recent-half vs. prior-half growth worth flagging

_PROPHET_MIN_POINTS = 14  # fewer days than this and a Prophet fit isn't trustworthy
_FORECAST_HORIZONS_DAYS = (30, 60, 90)

_DIMENSION_COLUMNS = ("service", "region", "instance_type", "project_tag", "cost_centre", "environment")


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


def _prophet_forecast(series: list[tuple[str, float]], horizon_days: int) -> dict:
    """One Prophet fit -> one horizon's projected total, with its confidence
    interval. Imports are local so importing this module never requires
    Prophet/pandas to be installed - only calling this function does."""
    import pandas as pd
    from prophet import Prophet

    df = pd.DataFrame(series, columns=["ds", "y"])
    # Daily cost data over a few weeks/months: weekly seasonality is
    # plausible (weekday vs. weekend usage patterns), daily/yearly are not
    # - too little data to fit either meaningfully at demo scale.
    model = Prophet(daily_seasonality=False, weekly_seasonality=True, yearly_seasonality=False)
    model.fit(df)

    future = model.make_future_dataframe(periods=horizon_days)
    forecast = model.predict(future)
    future_rows = forecast.tail(horizon_days)

    return {
        "horizon_days": horizon_days,
        "projected_total": round(float(future_rows["yhat"].clip(lower=0).sum()), 2),
        "projected_total_low": round(float(future_rows["yhat_lower"].clip(lower=0).sum()), 2),
        "projected_total_high": round(float(future_rows["yhat_upper"].clip(lower=0).sum()), 2),
    }


def forecast_costs(records: dict[str, list[dict]]) -> dict:
    """Project spend from the recent daily trend.

    Splits the observed daily series in half; the recent half's average daily
    run rate vs. the prior half's gives a trend percentage, applied to project
    a 30-day total. Returns ``{}`` (no opinion) when there isn't enough daily
    history to trust a trend — a flat/short series is not worth projecting.

    When there's enough history (``_PROPHET_MIN_POINTS``) and the ``prophet``
    package is installed, also adds a ``prophet_forecasts`` list — one entry
    per horizon in ``_FORECAST_HORIZONS_DAYS`` (30/60/90 days), each with a
    projected total and its confidence interval. A Prophet failure (not
    installed, or the fit itself raises) is silently absent, never an error -
    the trend-split result above always stands on its own.
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

    result = {
        "days_observed": len(series),
        "date_range": [series[0][0], series[-1][0]],
        "prior_period_daily_avg": round(prior_avg, 2),
        "recent_period_daily_avg": round(recent_avg, 2),
        "trend_pct": trend_pct,
        "projected_30d_total": projected_30d,
        "flag": trend_pct >= _TREND_FLAG_PCT,
    }

    if len(series) >= _PROPHET_MIN_POINTS:
        try:
            result["prophet_forecasts"] = [_prophet_forecast(series, h) for h in _FORECAST_HORIZONS_DAYS]
        except ImportError:
            pass  # prophet/pandas not installed - trend-split result stands alone
        except Exception:
            pass  # a bad fit (e.g. degenerate series) must never break the base result

    return result


def _dimension_recent_daily_avg(
    records: dict[str, list[dict]],
    table: str,
    dimension: str,
    value: str,
    window_days: int = 14,
) -> float | None:
    """This (table, dimension, value)'s own average daily cost over its most
    recent `window_days` observed days - the baseline what-if scenarios
    scale down from. None if that combination has no cost history at all."""
    rows = (records or {}).get(table) or []
    if not rows or dimension not in (rows[0] if rows else {}):
        return None

    by_date: dict[str, float] = defaultdict(float)
    for row in rows:
        if str(row.get(dimension)) != str(value):
            continue
        cost, date = row.get("cost"), row.get("date")
        if isinstance(cost, (int, float)) and date:
            by_date[str(date)] += float(cost)

    if not by_date:
        return None
    recent_dates = sorted(by_date)[-window_days:]
    recent_costs = [by_date[d] for d in recent_dates]
    return sum(recent_costs) / len(recent_costs)


def what_if_scenarios(records: dict[str, list[dict]], scenarios: list[dict]) -> list[dict]:
    """Evaluate simple what-if actions against each dimension value's own
    recent daily average cost - per the M3 Week 6 spec ("parameterise by
    'move X service to reserved' or 'shut down Y environment' and calculate
    projected savings").

    Each scenario dict:
        {"type": "shutdown" | "reserved_discount",
         "table": str, "dimension": str, "value": str,
         "discount_pct": float}  # only used by "reserved_discount"
        optional "horizon_days" (default 30)

    Returns one result per scenario, in the same order, each carrying the
    scenario's own fields plus baseline_daily_avg/projected_savings/note - or
    an "error" key (never raises) if that (table, dimension, value) has no
    cost history to scale down from.
    """
    results = []
    for scenario in scenarios:
        table = scenario.get("table")
        dimension = scenario.get("dimension")
        value = scenario.get("value")
        scenario_type = scenario.get("type")
        horizon_days = scenario.get("horizon_days", 30)

        baseline = _dimension_recent_daily_avg(records, table, dimension, value)
        if baseline is None:
            results.append({**scenario, "error": f"No cost history found for {table}.{dimension}={value}"})
            continue

        if scenario_type == "shutdown":
            savings = baseline * horizon_days
            note = f"Shutting down {value} would avoid its full recent daily average (${baseline:.2f}/day)."
        elif scenario_type == "reserved_discount":
            discount_pct = scenario.get("discount_pct", 0.0)
            savings = baseline * horizon_days * (discount_pct / 100.0)
            note = f"Moving {value} to reserved at a {discount_pct:.0f}% discount off its recent " \
                   f"${baseline:.2f}/day average."
        else:
            results.append({**scenario, "error": f"Unknown scenario type '{scenario_type}'"})
            continue

        results.append({
            **scenario,
            "baseline_daily_avg": round(baseline, 2),
            "horizon_days": horizon_days,
            "projected_savings": round(savings, 2),
            "note": note,
        })

    return results
