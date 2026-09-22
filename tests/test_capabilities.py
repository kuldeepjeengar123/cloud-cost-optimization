"""Tests for the deterministic, no-LLM capability modules added this session:
forecasting, tag_governance, and root_cause (which correlates against
anomaly_detection's structured findings). These are pure functions of
already-normalized records, so no LLM, network, or AWS access is needed.

Run with: .venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime
import unittest

from src.capabilities.anomaly_detection import detect_anomalies
from src.capabilities.forecasting import forecast_costs
from src.capabilities.root_cause import explain_anomalies
from src.capabilities.tag_governance import check_tag_governance


def _daily_records(day_costs: dict[int, float], project_tag: str = "analytics-1", service: str = "ec2") -> dict:
    """Build a single-table `service_daily_cost`-shaped records dict, one row
    per (day index -> cost) entry, starting 2026-01-01."""
    base = datetime.date(2026, 1, 1)
    rows = []
    for i, cost in day_costs.items():
        rows.append({
            "date": (base + datetime.timedelta(days=i)).isoformat(),
            "cost": cost,
            "service": service,
            "project_tag": project_tag,
        })
    return {"service_daily_cost": rows}


class ForecastCostsTests(unittest.TestCase):
    def test_flags_material_upward_trend(self):
        # First half flat at $10/day, second half flat at $20/day -> +100% trend.
        records = _daily_records({i: (10.0 if i < 5 else 20.0) for i in range(10)})
        forecast = forecast_costs(records)
        self.assertTrue(forecast["flag"])
        self.assertEqual(forecast["trend_pct"], 100)
        self.assertAlmostEqual(forecast["projected_30d_total"], 20.0 * 30)

    def test_no_opinion_below_minimum_history(self):
        # Only 3 days of history — below _MIN_POINTS (6) — must return {} rather
        # than a misleading trend from too little data.
        records = _daily_records({0: 10.0, 1: 12.0, 2: 9.0})
        self.assertEqual(forecast_costs(records), {})

    def test_empty_records_returns_empty(self):
        self.assertEqual(forecast_costs({}), {})
        self.assertEqual(forecast_costs({"service_daily_cost": []}), {})


class CheckTagGovernanceTests(unittest.TestCase):
    def test_flags_missing_project_tag_with_cost_exposure(self):
        records = {"service_daily_cost": [
            {"date": "2026-01-01", "cost": 90.0, "project_tag": "analytics-1"},
            {"date": "2026-01-02", "cost": 10.0, "project_tag": ""},  # untagged
        ]}
        findings = check_tag_governance(records)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["rows_affected"], 1)
        self.assertAlmostEqual(finding["cost_exposed"], 10.0)
        self.assertEqual(finding["severity"], "medium")  # 10% share, below the 20% high-severity bar

    def test_no_findings_when_fully_tagged(self):
        records = {"service_daily_cost": [
            {"date": "2026-01-01", "cost": 50.0, "project_tag": "analytics-1"},
            {"date": "2026-01-02", "cost": 50.0, "project_tag": "web-prod-1"},
        ]}
        self.assertEqual(check_tag_governance(records), [])

    def test_no_tag_column_present_is_skipped_not_flagged(self):
        # A table with no project_tag column at all must not be treated as
        # "everything untagged" — there is nothing to check.
        records = {"region_cost": [{"date": "2026-01-01", "cost": 10.0, "region": "us-east-1"}]}
        self.assertEqual(check_tag_governance(records), [])


class ExplainAnomaliesTests(unittest.TestCase):
    def test_attaches_driver_from_correlated_dimension(self):
        # A spike on day 8, concentrated in project_tag=analytics-1 within the
        # same table/date — root_cause should surface that as the driver.
        base = datetime.date(2026, 1, 1)
        rows = []
        for i in range(10):
            date = (base + datetime.timedelta(days=i)).isoformat()
            spike_cost = 40.0 if i == 8 else 10.0
            rows.append({"date": date, "cost": spike_cost, "service": "ec2", "project_tag": "analytics-1"})
            rows.append({"date": date, "cost": 5.0, "service": "s3", "project_tag": "web-prod-1"})
        records = {"service_daily_cost": rows}

        anomalies = detect_anomalies(records)
        self.assertTrue(anomalies, "expected the detector to flag the day-8 spike")
        explained = explain_anomalies(records, anomalies)
        spike = next(a for a in explained if a.get("date") == (base + datetime.timedelta(days=8)).isoformat())
        self.assertIn("drivers", spike)
        self.assertEqual(spike["drivers"][0]["value"], "analytics-1")
        self.assertEqual(spike["drivers"][0]["dimension"], "project_tag")

    def test_passthrough_when_no_correlated_dimension_available(self):
        # A table with only date+cost+service (no other dimension column) can't
        # be correlated against anything — must pass findings through unchanged,
        # not raise.
        base = datetime.date(2026, 1, 1)
        rows = [{"date": (base + datetime.timedelta(days=i)).isoformat(),
                  "cost": 40.0 if i == 8 else 10.0, "service": "ec2"} for i in range(10)]
        records = {"service_daily_cost": rows}
        anomalies = detect_anomalies(records)
        explained = explain_anomalies(records, anomalies)
        self.assertEqual(len(explained), len(anomalies))
        self.assertNotIn("drivers", explained[0])

    def test_empty_signals_returns_empty(self):
        self.assertEqual(explain_anomalies({"service_daily_cost": []}, []), [])


if __name__ == "__main__":
    unittest.main()
