from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent_module.pipeline import build_pipeline_graph

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SAMPLE_M1_OUTPUT = Path("sample_data/m1_sample_output.json")

_STEP_LABELS = [
    "State BEFORE Step 1 (normalise)",
    "State AFTER Step 1 (normalise)",
    "State AFTER Step 2 (enrich)",
]


def print_state(label: str, state: dict) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(state, indent=2, default=str))


def run_once(graph, raw_record: dict, run_label: str) -> None:
    print("=" * 70)
    print(run_label)
    print("=" * 70)

    initial_state = {
        "raw_record": raw_record,
        "normalized_record": None,
        "cache_hit": None,
        "normalise_latency_seconds": None,
        "business_tags": None,
        "pricing_context": None,
    }

    for i, state in enumerate(graph.stream(initial_state, stream_mode="values")):
        label = _STEP_LABELS[i] if i < len(_STEP_LABELS) else f"State after step {i}"
        print_state(label, state)


def main() -> None:
    load_dotenv()
    graph = build_pipeline_graph()

    with _SAMPLE_M1_OUTPUT.open("r", encoding="utf-8") as f:
        raw_record = json.load(f)

    run_once(graph, raw_record, "RUN 1 — first time seeing this record (expect a real LLM call)")
    run_once(graph, raw_record, "RUN 2 — same record again (expect a semantic cache hit, no LLM call)")


if __name__ == "__main__":
    main()
