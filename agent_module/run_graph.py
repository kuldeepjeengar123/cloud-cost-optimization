from __future__ import annotations

import sys

from agent_module.graph import build_graph

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_query(graph, query: str) -> None:
    print("=" * 60)
    print(f"Query: {query}")
    print("=" * 60)
    initial_state = {"query": query, "query_type": None, "result": None}
    for state in graph.stream(initial_state, stream_mode="values"):
        print(state)
    print()


def main() -> None:
    graph = build_graph()

    print("Graph structure (Mermaid):\n")
    print(graph.get_graph().draw_mermaid())

    run_query(graph, "Why is EC2 spend anomalously high this month?")
    run_query(graph, "Forecast our S3 spend for next month.")


if __name__ == "__main__":
    main()
