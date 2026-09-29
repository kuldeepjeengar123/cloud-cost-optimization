"""Quickest visible M1 -> M2 -> M3 -> M4 dry run, plus an exact LLM-call
count estimate - no AWS calls, no real LLM calls (FakeLLMClient substitutes
for the batch pipeline's own LLM steps; M2 enrichment stays off unless you
edit ENABLE_M2_ENRICHMENT below and have a real OPENROUTER_API_KEY).

Run: python demo_dry_run.py
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from src.config import PipelineConfig
from src.orchestrator_graph import run_pipeline_graph

ENABLE_M2_ENRICHMENT = False  # flip only once agents/.env has a real key


class FakeLLMClient:
    """Every call raises - proves the real graph wiring / fallback paths
    without spending anything. Same technique tests/test_orchestrator_graph.py
    already uses."""

    def __init__(self, *a, **k):
        pass

    def complete(self, *a, **k):
        raise RuntimeError("fake LLM: dry run, no real LLM calls")

    def complete_json(self, *a, **k):
        raise RuntimeError("fake LLM: dry run, no real LLM calls")


def _count_m1_records(m1_folder: Path) -> tuple[int, int]:
    """(total line items, unique line items) currently in M1's real staging
    output - unique is what M2's semantic cache would actually call the LLM
    for; a repeat is a free cache hit."""
    key_path = m1_folder / "secret.key"
    staging = m1_folder / "staging"
    if not key_path.exists() or not staging.exists():
        return 0, 0

    fernet = Fernet(key_path.read_bytes())
    items: list[dict] = []
    for enc_path in sorted(staging.glob("*.json.enc")):
        items.extend(json.loads(fernet.decrypt(enc_path.read_bytes()).decode("utf-8")))

    unique = {json.dumps(item, sort_keys=True) for item in items}
    return len(items), len(unique)


def print_llm_call_estimate(cfg: PipelineConfig) -> None:
    total, unique = _count_m1_records(cfg.m1_folder)

    print("=" * 70)
    print("LLM CALL ESTIMATE (nothing below is actually called)")
    print("=" * 70)
    print(f"Real M1 line items on disk: {total} total, {unique} unique (by content)")
    print()
    print("Batch pipeline (src/orchestrator_graph.py) - ALWAYS these 4, regardless")
    print("of how much data there is, since each runs once per pipeline call:")
    print("  1. Step 1  simplify_normalize - schema-map proposal (1 call)")
    print("  2. Step 3.1 generate_charts    - chart specs        (1 call)")
    print("  3. Step 3.2 analyze_metrics    - analysis narrative (1 call)")
    print("  4. Step 3.3 write_summary      - executive summary  (1 call)")
    print(f"  Subtotal: 4 calls (cfg.capabilities.m2_enrichment = {cfg.capabilities.m2_enrichment})")
    print()
    if cfg.capabilities.m2_enrichment:
        print(f"M2 enrichment is ON - one LLM completion + one embedding call per")
        print(f"UNIQUE M1 record (repeats are free cache hits after the first run):")
        print(f"  {unique} unique record(s) x 2 calls = {unique * 2} calls")
        print(f"  Subtotal: {unique * 2} calls")
        print()
        print(f"GRAND TOTAL for this run: {4 + unique * 2} real calls "
              f"(well under the free-tier's 20/min and 50-1000/day caps)")
    else:
        print("M2 enrichment is OFF (cfg.capabilities.m2_enrichment = False, the default) -")
        print("M2's graph is never even constructed, so it makes zero calls this run.")
        print()
        print("GRAND TOTAL for this run: 4 real calls")
    print()


def run_visible_dry_run() -> None:
    cfg = PipelineConfig(user_query="Comprehensive cost review")
    cfg.teams_webhook_url = ""
    cfg.capabilities.m2_enrichment = ENABLE_M2_ENRICHMENT

    print_llm_call_estimate(cfg)

    print("=" * 70)
    print(f"RUNNING THE REAL PIPELINE (FakeLLMClient - zero calls actually made)")
    print("=" * 70)

    with patch("src.orchestrator_graph.LLMClient", FakeLLMClient):
        result = run_pipeline_graph(cfg)

    analysis = result["payload"].get("analysis", {})
    anomalies = analysis.get("anomalies", [])
    methods = sorted({a.get("method") for a in anomalies if "method" in a})

    print("\n" + "=" * 70)
    print("RESULT SUMMARY")
    print("=" * 70)
    print(f"M1 input used         : {cfg.m1_folder} (sources={cfg.sources})")
    print(f"Anomalies found       : {len(anomalies)}  (detector methods fired: {methods or 'none - see note below'})")
    print(f"Forecast produced     : {bool(analysis.get('forecast'))}")
    print(f"Actions planned (M4)  : {len(result['actions'])}")
    print(f"Report (plaintext)    : {Path(result['json_path']).name}")
    print(f"Report (markdown)     : {Path(result['markdown_path']).name}")
    print(f"Report (encrypted)    : {Path(result.get('encrypted_path', '')).name or '(output_guardrails off)'}")
    if not anomalies:
        print("\nNote: 0 anomalies is expected, not a bug - M3's anomaly/forecast")
        print("detectors require more distinct dates than the current small M1")
        print("demo dataset has (see integration Step 6's minimum-data guards).")

    print("\n" + "=" * 70)
    print("WHAT THIS DRY RUN DID NOT TOUCH")
    print("=" * 70)
    print("Redis and Postgres are M2's backing infra (session memory + semantic")
    print("cache in Redis; LangGraph checkpointer + M3's persistent schema in")
    print("Postgres). Since M2 enrichment is off and nothing here calls M2's")
    print("graph, neither one gets written to by this run - checked separately,")
    print("not assumed.")


if __name__ == "__main__":
    run_visible_dry_run()
