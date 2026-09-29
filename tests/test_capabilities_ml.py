"""Tests for the M3 Week 5/6 ML-backed additions: Isolation Forest anomaly
detection, Prophet forecasting, and what-if scenarios. Kept separate from
test_capabilities.py because Prophet fits are slow (several seconds each) -
these should not slow down the fast core suite on every run.

No LLM, network, or AWS access needed - scikit-learn and Prophet both run
entirely locally. Requires scikit-learn and prophet to be installed; skipped
automatically if they aren't (matching this module's own "degrade, don't
break" contract for optional heavier dependencies).

Run with: .venv/Scripts/python.exe -m unittest discover -s tests -p "test_capabilities_ml.py" -v
"""

from __future__ import annotations

import datetime
import unittest

from src.capabilities.anomaly_detection import detect_anomalies
from src.capabilities.forecasting import forecast_costs, what_if_scenarios

try:
    import sklearn  # noqa: F401
    _HAVE_SKLEARN = True
except ImportError:
    _HAVE_SKLEARN = False

try:
    import prophet  # noqa: F401
    _HAVE_PROPHET = True
except ImportError:
    _HAVE_PROPHET = False


def _multi_dimensional_records(spike_day: int = 15, num_days: int = 30) -> dict:
    """Build 4 tables (ec2_instance_cost, region_cost, service_daily_cost,
    tag_cost) with distinct dimension columns, mirroring docs/*.csv's real
    shape, with a joint anomaly on `spike_day`: EVERY dimension value ticks
    up a little that day - individually unremarkable (well under the
    z-score detector's threshold), but jointly unusual across the whole
    cost picture, which is exactly what the Isolation Forest pass is for."""
    base = datetime.date(2026, 1, 1)
    ec2_rows, region_rows, service_rows, tag_rows = [], [], [], []

    for i in range(num_days):
        date = (base + datetime.timedelta(days=i)).isoformat()
        bump = 1.3 if i == spike_day else 1.0  # +30% across the board that day only

        for instance_type, base_cost in (("m5.xlarge", 8.0), ("r5.large", 3.0)):
            ec2_rows.append({"date": date, "instance_type": instance_type, "cost": base_cost * bump})
        for region, base_cost in (("us-east-1", 5.0), ("eu-west-2", 2.5)):
            region_rows.append({"date": date, "region": region, "cost": base_cost * bump})
        for service, base_cost in (("AmazonEC2", 6.0), ("AmazonS3", 1.5)):
            service_rows.append({"date": date, "service": service, "cost": base_cost * bump})
        for tag, base_cost in (("analytics-1", 4.0), ("web-prod-1", 2.0)):
            tag_rows.append({"date": date, "project_tag": tag, "cost": base_cost * bump})

    return {
        "ec2_instance_cost": ec2_rows,
        "region_cost": region_rows,
        "service_daily_cost": service_rows,
        "tag_cost": tag_rows,
    }


@unittest.skipUnless(_HAVE_SKLEARN, "scikit-learn not installed")
class IsolationForestAnomalyTests(unittest.TestCase):
    def test_flags_joint_multidimensional_spike(self):
        records = _multi_dimensional_records(spike_day=15)
        findings = detect_anomalies(records)

        isolation_findings = [f for f in findings if f.get("method") == "isolation_forest"]
        self.assertTrue(isolation_findings, "expected at least one isolation_forest finding")

        base = datetime.date(2026, 1, 1)
        spike_date = (base + datetime.timedelta(days=15)).isoformat()
        self.assertTrue(
            any(f["date"] == spike_date for f in isolation_findings),
            f"expected a finding on the spike day {spike_date}, got dates: "
            f"{[f['date'] for f in isolation_findings]}",
        )

    def test_zscore_findings_still_present_and_unchanged_shape(self):
        # The original z-score detector's own findings must still carry
        # their pre-upgrade fields (backward compatible for root_cause.py).
        records = _multi_dimensional_records(spike_day=15)
        findings = detect_anomalies(records)
        zscore_findings = [f for f in findings if f.get("method") == "zscore"]
        for f in zscore_findings:
            self.assertIn("table", f)
            self.assertIn("dimension", f)
            self.assertIn("cost", f)
            self.assertIn("avg", f)

    def test_too_little_data_yields_no_isolation_forest_findings(self):
        # Below _IF_MIN_DATES - must not error, must just skip the ML pass.
        records = _multi_dimensional_records(spike_day=2, num_days=5)
        findings = detect_anomalies(records)
        self.assertFalse(any(f.get("method") == "isolation_forest" for f in findings))

    def test_single_dimension_column_yields_no_isolation_forest_findings(self):
        # Only one (table, value) column of variation - not "multi-dimensional"
        # - below _IF_MIN_FEATURES, must not error, must just skip the ML pass.
        base = datetime.date(2026, 1, 1)
        rows = [{"date": (base + datetime.timedelta(days=i)).isoformat(),
                 "cost": 40.0 if i == 8 else 10.0, "service": "ec2"} for i in range(15)]
        findings = detect_anomalies({"service_daily_cost": rows})
        self.assertFalse(any(f.get("method") == "isolation_forest" for f in findings))


class WhatIfScenarioTests(unittest.TestCase):
    def _records(self) -> dict:
        base = datetime.date(2026, 1, 1)
        rows = [
            {"date": (base + datetime.timedelta(days=i)).isoformat(), "instance_type": "m5.xlarge", "cost": 10.0}
            for i in range(14)
        ]
        return {"ec2_instance_cost": rows}

    def test_shutdown_scenario_projects_full_savings(self):
        records = self._records()
        results = what_if_scenarios(records, [
            {"type": "shutdown", "table": "ec2_instance_cost", "dimension": "instance_type", "value": "m5.xlarge"},
        ])
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertNotIn("error", result)
        self.assertAlmostEqual(result["baseline_daily_avg"], 10.0)
        self.assertAlmostEqual(result["projected_savings"], 10.0 * 30)  # default horizon

    def test_reserved_discount_scenario_projects_partial_savings(self):
        records = self._records()
        results = what_if_scenarios(records, [
            {"type": "reserved_discount", "table": "ec2_instance_cost", "dimension": "instance_type",
             "value": "m5.xlarge", "discount_pct": 40, "horizon_days": 60},
        ])
        result = results[0]
        self.assertAlmostEqual(result["projected_savings"], 10.0 * 60 * 0.40)

    def test_unknown_dimension_value_returns_error_not_exception(self):
        records = self._records()
        results = what_if_scenarios(records, [
            {"type": "shutdown", "table": "ec2_instance_cost", "dimension": "instance_type", "value": "does-not-exist"},
        ])
        self.assertIn("error", results[0])

    def test_unknown_scenario_type_returns_error_not_exception(self):
        records = self._records()
        results = what_if_scenarios(records, [
            {"type": "not_a_real_type", "table": "ec2_instance_cost", "dimension": "instance_type", "value": "m5.xlarge"},
        ])
        self.assertIn("error", results[0])


@unittest.skipUnless(_HAVE_PROPHET, "prophet not installed")
class ProphetForecastTests(unittest.TestCase):
    def test_adds_prophet_forecasts_with_enough_history(self):
        base = datetime.date(2026, 1, 1)
        rows = [
            {"date": (base + datetime.timedelta(days=i)).isoformat(), "cost": 10.0 + (i % 7), "service": "ec2"}
            for i in range(30)
        ]
        forecast = forecast_costs({"service_daily_cost": rows})
        self.assertIn("prophet_forecasts", forecast)
        horizons = {pf["horizon_days"] for pf in forecast["prophet_forecasts"]}
        self.assertEqual(horizons, {30, 60, 90})
        for pf in forecast["prophet_forecasts"]:
            self.assertGreaterEqual(pf["projected_total"], 0)
            self.assertLessEqual(pf["projected_total_low"], pf["projected_total"])
            self.assertGreaterEqual(pf["projected_total_high"], pf["projected_total"])

    def test_no_prophet_forecasts_below_minimum_history(self):
        # Enough for the base trend-split (_MIN_POINTS=6) but not for
        # Prophet (_PROPHET_MIN_POINTS=14) - base result present, no
        # prophet_forecasts key, and no error either.
        base = datetime.date(2026, 1, 1)
        rows = [
            {"date": (base + datetime.timedelta(days=i)).isoformat(), "cost": 10.0, "service": "ec2"}
            for i in range(8)
        ]
        forecast = forecast_costs({"service_daily_cost": rows})
        self.assertNotEqual(forecast, {})
        self.assertNotIn("prophet_forecasts", forecast)


if __name__ == "__main__":
    unittest.main()
