from __future__ import annotations

import json
import sys

from dotenv import load_dotenv
from langgraph.types import Command

from agent_module.intent_pipeline import build_intent_pipeline_graph

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SAMPLE_RECORD = {
    "svc": "Amazon Elastic Compute Cloud - Compute",
    "acct_id": "123456789012",
    "rgn": None,
    "cost": "1523.47",
    "curr": "USD",
    "period_start": "2026-06-01",
    "period_end": "2026-06-30",
    "resource": "i-0ec2used123",
    "env_tag": None,
    "usage_qty": "720",
    "usage_unit": "Hrs",
}


def _base_state(request: str) -> dict:
    return {
        "raw_record": _SAMPLE_RECORD,
        "request": request,
        "normalized_record": None,
        "cache_hit": None,
        "normalise_latency_seconds": None,
        "business_tags": None,
        "pricing_context": None,
        "intent": None,
        "tool_result": None,
        "path": [],
        "retry_count": 0,
        "issue_found": None,
        "issue_reason": None,
        "human_decision": None,
    }


def print_result(result: dict) -> None:
    print(f"Graph path taken : {' -> '.join(result.get('path', []))}")
    print(f"Detected intent  : {result.get('intent')}")
    print(f"Retry count      : {result.get('retry_count')}")
    print(f"Tool result      : {json.dumps(result.get('tool_result'), indent=2)}")
    if result.get("human_decision") is not None:
        print(f"Human decision   : {result.get('human_decision')}")


def run_case(graph, thread_id: str, run_label: str, request: str) -> None:
    print("=" * 70)
    print(run_label)
    print(f"Request: {request!r}")
    print("=" * 70)

    config = {"configurable": {"thread_id": thread_id}}
    state = _base_state(request)
    result = graph.invoke(state, config=config)

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("\n--- PAUSED: human_interrupt ---")
        print("The graph has genuinely stopped here and is waiting for a real decision.")
        print(json.dumps(payload, indent=2, default=str))
        human_response = input("\nApprove this finding? Type 'yes' to continue (or any other response to record it as-is): ")
        result = graph.invoke(Command(resume=human_response), config=config)

    print()
    print_result(result)
    print()


def main() -> None:
    load_dotenv()
    with build_intent_pipeline_graph() as graph:
        run_case(
            graph,
            "demo-cost-anomaly",
            "RUN 1 — cost_anomaly",
            "Why is this spend so high?",
        )

        run_case(
            graph,
            "demo-budget-forecast",
            "RUN 2 — budget_forecast",
            "What will this cost next month?",
        )

        run_case(
            graph,
            "demo-optimisation",
            "RUN 3 — optimisation_recommendation",
            "How can we reduce this cost?",
        )

        run_case(
            graph,
            "demo-usage-report",
            "RUN 4 — usage_report",
            "Give me a usage summary for this record.",
        )


if __name__ == "__main__":
    main()
