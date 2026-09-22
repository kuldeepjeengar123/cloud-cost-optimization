"""Tests for src/orchestrator_graph.py — the LangGraph rebuild of
src/orchestrator.py's run_pipeline(), built alongside it (not yet wired into
server.py/main.py) per the "build alongside, then switch" migration plan.

Uses a fake LLMClient (every call raises) instead of a real network call —
every LLM-calling pipeline step already tolerates a failed call and falls
back to a documented default (see step1_normalize.py, step3_1_charts.py,
step3_2_analysis.py, step3_3_summary.py), so this genuinely exercises the
real graph wiring / state-merging / event contract without any LLM cost.
Local CSV input (docs/*.csv, already in this repo) is used for real, since
reading it costs nothing.

Run with: .venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from src.config import PipelineConfig
from src.orchestrator import STEP_LABELS
from src.orchestrator_graph import run_pipeline_graph


class FakeLLMClient:
    """Every call raises — each pipeline step already has a documented,
    tested fallback for a failed LLM call, so this exercises those paths
    for free instead of needing a hand-authored JSON response per prompt."""

    def __init__(self, *args, **kwargs):
        pass

    def complete(self, *args, **kwargs):
        raise RuntimeError("fake LLM: no network in tests")

    def complete_json(self, *args, **kwargs):
        raise RuntimeError("fake LLM: no network in tests")


def _test_cfg() -> PipelineConfig:
    cfg = PipelineConfig(user_query="test query", sources=["local_csv"])
    cfg.teams_webhook_url = ""  # never actually post to Teams from a test
    # actions.risk.assess_risk() constructs its OWN LLMClient independent of
    # the FakeLLMClient patched into orchestrator_graph's namespace below —
    # patching that module's LLMClient does not reach it. Disabling the
    # feature via its own documented toggle (rather than patching risk.py
    # too) is the safest way to guarantee zero real network/LLM calls here.
    cfg.capabilities.assess_risk = False
    return cfg


class RunPipelineGraphTests(unittest.TestCase):
    @patch("src.orchestrator_graph.LLMClient", FakeLLMClient)
    def test_runs_to_completion_with_expected_result_shape(self):
        cfg = _test_cfg()
        result = run_pipeline_graph(cfg)
        # Same top-level keys orchestrator.run_pipeline produces.
        for key in ("run_id", "actions", "action_backend", "applied_folder",
                    "json_path", "markdown_path"):
            self.assertIn(key, result, f"missing '{key}' in run_pipeline_graph's result")

    @patch("src.orchestrator_graph.LLMClient", FakeLLMClient)
    def test_emits_start_before_complete_for_every_step_label(self):
        cfg = _test_cfg()
        events: list[tuple[str, dict]] = []
        run_pipeline_graph(cfg, on_event=lambda kind, payload: events.append((kind, payload)))

        started = {p["step"] for k, p in events if k == "step_start"}
        completed = {p["step"] for k, p in events if k == "step_complete"}
        self.assertEqual(started, set(STEP_LABELS.keys()))
        self.assertEqual(completed, set(STEP_LABELS.keys()))

        # A step_start must precede its own step_complete, for every step.
        # (Genuine concurrency for the three agent steps is verified
        # separately in ParallelAgentConcurrencyTests below via timing —
        # asserting a strict start/complete *interleaving order* here would
        # be flaky, since forecast/tag_governance/root_cause each finish in
        # sub-millisecond time and thread-scheduling jitter alone can put one
        # thread's "complete" ahead of another thread's "start" in the
        # shared events list with no bearing on whether they truly ran
        # concurrently.)
        start_index = {p["step"]: i for i, (k, p) in enumerate(events) if k == "step_start"}
        complete_index = {p["step"]: i for i, (k, p) in enumerate(events) if k == "step_complete"}
        for step in STEP_LABELS:
            self.assertLess(start_index[step], complete_index[step], f"{step}: start did not precede complete")


class ParallelAgentConcurrencyTests(unittest.TestCase):
    def test_three_agents_run_concurrently_not_sequentially(self):
        """Patches the three agent runners (imported into orchestrator_graph's
        namespace) with a 0.3s sleep each; sequential execution would take
        >=0.9s, real concurrency should take well under that — the same
        timing technique used to verify LangGraph's real parallelism during
        this feature's own research (see the migration plan)."""
        cfg = _test_cfg()

        def slow_forecast(records, cfg):
            time.sleep(0.3)
            return {}

        def slow_tag_governance(records, cfg):
            time.sleep(0.3)
            return []

        def slow_root_cause(records, signals, cfg):
            time.sleep(0.3)
            return signals

        with patch("src.orchestrator_graph.LLMClient", FakeLLMClient), \
             patch("src.orchestrator_graph.run_forecast_agent", slow_forecast), \
             patch("src.orchestrator_graph.run_tag_governance_agent", slow_tag_governance), \
             patch("src.orchestrator_graph.run_root_cause_agent", slow_root_cause):
            t0 = time.time()
            run_pipeline_graph(cfg)
            elapsed = time.time() - t0

        self.assertLess(elapsed, 0.8, "agents did not appear to run concurrently")


if __name__ == "__main__":
    unittest.main()
