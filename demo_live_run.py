"""Live M1 -> M2 -> M3 -> M4 run: makes REAL LLM/embedding calls via OpenRouter's
free-tier models. Nothing here runs on import - you must explicitly flip the
two flags below and re-run. Companion to demo_dry_run.py (which fakes every
LLM call); this one doesn't fake anything, so read print_llm_call_estimate's
output before confirming.

Prerequisites:
  - Docker containers up (redis + postgres) - see docker-compose.yml
  - A real OPENROUTER_API_KEY in BOTH .env (repo root, read by src/config.py)
    and agents/.env (read when M2's graph is built, if ENABLE_M2_ENRICHMENT)

Run: python demo_live_run.py
"""
from __future__ import annotations

from src.config import PipelineConfig
from src.orchestrator_graph import run_pipeline_graph
from demo_dry_run import print_llm_call_estimate, _count_m1_records

# Flip independently - see the table in this file's module docstring.
ENABLE_M2_ENRICHMENT = True   # M2's real completion + embedding call, per unique M1 record
ENABLE_BATCH_LLM_CALLS = True  # the pipeline's own 4 calls (normalize/charts/analysis/summary)

_CONFIRM_TEXT = "yes, spend real credits"


def run_live() -> None:
    cfg = PipelineConfig(user_query="Comprehensive cost review")
    cfg.teams_webhook_url = ""
    cfg.capabilities.m2_enrichment = ENABLE_M2_ENRICHMENT

    print_llm_call_estimate(cfg)

    if not ENABLE_M2_ENRICHMENT and not ENABLE_BATCH_LLM_CALLS:
        print("Both flags are False - this would make zero real calls. Nothing to run.")
        print("Set ENABLE_M2_ENRICHMENT and/or ENABLE_BATCH_LLM_CALLS to True above, then re-run.")
        return

    total, unique = _count_m1_records(cfg.m1_folder)
    expected = (4 if ENABLE_BATCH_LLM_CALLS else 0) + (unique * 2 if ENABLE_M2_ENRICHMENT else 0)
    print(f"About to make {expected} REAL call(s) against OpenRouter's free-tier models.")
    typed = input(f"Type '{_CONFIRM_TEXT}' to proceed: ").strip()
    if typed != _CONFIRM_TEXT:
        print("Not confirmed - aborting, no calls made.")
        return

    if not ENABLE_BATCH_LLM_CALLS:
        from unittest.mock import patch

        class FakeLLMClient:
            def __init__(self, *a, **k):
                pass

            def complete(self, *a, **k):
                raise RuntimeError("fake LLM: ENABLE_BATCH_LLM_CALLS is False")

            def complete_json(self, *a, **k):
                raise RuntimeError("fake LLM: ENABLE_BATCH_LLM_CALLS is False")

        with patch("src.orchestrator_graph.LLMClient", FakeLLMClient):
            result = run_pipeline_graph(cfg)
    else:
        result = run_pipeline_graph(cfg)

    payload = result["payload"]
    analysis = payload.get("analysis", {})
    correlations = payload.get("correlations", {})
    print("\n" + "=" * 70)
    print("RESULT SUMMARY (real calls made where enabled above)")
    print("=" * 70)
    print(f"Anomalies found       : {len(analysis.get('anomalies', []))}")
    print(f"Cost-centre breakdown : {correlations.get('cost_centre_cost__by_cost_centre')}")
    print(f"Environment breakdown : {correlations.get('environment_cost__by_environment')}")
    print(f"Report (plaintext)    : {result['json_path']}")
    print(f"Report (encrypted)    : {result.get('encrypted_path', '(output_guardrails off)')}")


if __name__ == "__main__":
    run_live()
