"""Tests for the orchestrator's parallel agent runners' "never raise" contract
(src/orchestrator.py's run_forecast_agent / run_tag_governance_agent /
run_root_cause_agent). This is the fix for the finding that the ThreadPoolExecutor
block's future.result() calls were previously unguarded: if any of the three
underlying capability functions raised, the whole pipeline run would crash
instead of degrading gracefully, unlike every other step in this pipeline
(see risk.py's assess_risk and the step3_x fallback paths).

Run with: .venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.config import PipelineConfig
from src.orchestrator import (
    run_forecast_agent,
    run_root_cause_agent,
    run_tag_governance_agent,
)


class AgentRunnersNeverRaiseTests(unittest.TestCase):
    def setUp(self):
        self.cfg = PipelineConfig()  # defaults: every capabilities.* toggle is True

    def test_forecast_agent_survives_underlying_exception(self):
        with patch("src.orchestrator.forecast_costs", side_effect=RuntimeError("boom")):
            result = run_forecast_agent({"service_daily_cost": []}, self.cfg)
        self.assertEqual(result, {})  # safe default, not a raised exception

    def test_tag_governance_agent_survives_underlying_exception(self):
        with patch("src.orchestrator.check_tag_governance", side_effect=RuntimeError("boom")):
            result = run_tag_governance_agent({"service_daily_cost": []}, self.cfg)
        self.assertEqual(result, [])

    def test_root_cause_agent_survives_underlying_exception(self):
        signals = [{"finding": "spike", "table": "service_daily_cost", "date": "2026-01-01"}]
        with patch("src.orchestrator.explain_anomalies", side_effect=RuntimeError("boom")):
            result = run_root_cause_agent({"service_daily_cost": []}, signals, self.cfg)
        # Falls back to the original (un-explained) signals, not an exception.
        self.assertEqual(result, signals)

    def test_forecast_agent_respects_capability_toggle(self):
        cfg = PipelineConfig()
        cfg.capabilities.forecast_costs = False
        with patch("src.orchestrator.forecast_costs") as mocked:
            result = run_forecast_agent({"service_daily_cost": []}, cfg)
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_root_cause_agent_skips_work_when_no_signals(self):
        with patch("src.orchestrator.explain_anomalies") as mocked:
            result = run_root_cause_agent({"service_daily_cost": []}, [], self.cfg)
        mocked.assert_not_called()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
