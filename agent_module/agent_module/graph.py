from __future__ import annotations

from typing import Optional

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict


class QueryState(TypedDict):
    """Shared state that flows through every node in the graph."""

    query: str
    query_type: Optional[str]
    result: Optional[str]


def receive_input(state: QueryState) -> dict:
    """Node 1: receive the incoming query and normalize it."""
    return {"query": state["query"].strip()}


def process(state: QueryState) -> dict:
    """Node 2: classify the query as 'anomaly' or 'forecast' based on its wording."""
    text = state["query"].lower()
    forecast_keywords = ("forecast", "predict", "projection", "next month", "future")
    query_type = "forecast" if any(k in text for k in forecast_keywords) else "anomaly"
    return {"query_type": query_type}


def route_by_query_type(state: QueryState) -> str:
    """Conditional edge: choose the next node based on state['query_type']."""
    return "handle_forecast" if state["query_type"] == "forecast" else "handle_anomaly"


def handle_anomaly(state: QueryState) -> dict:
    """Branch node: handles queries classified as anomaly-detection questions."""
    return {"result": f"Checked '{state['query']}' against typical spend patterns and flagged it as anomalous."}


def handle_forecast(state: QueryState) -> dict:
    """Branch node: handles queries classified as forecasting questions."""
    return {"result": f"Projected the future spend trend for '{state['query']}' based on historical usage."}


def emit_output(state: QueryState) -> dict:
    """Node 3: final node, formats the branch result for output."""
    return {"result": f"FINAL ANSWER ({state['query_type']}): {state['result']}"}


def build_graph() -> CompiledStateGraph:
    """Wire the 3-node backbone (receive_input -> process -> emit_output) with a
    conditional edge after 'process' that routes to one of two branch nodes
    before reaching emit_output."""
    graph = StateGraph(QueryState)

    graph.add_node("receive_input", receive_input)
    graph.add_node("process", process)
    graph.add_node("handle_anomaly", handle_anomaly)
    graph.add_node("handle_forecast", handle_forecast)
    graph.add_node("emit_output", emit_output)

    graph.add_edge(START, "receive_input")
    graph.add_edge("receive_input", "process")
    graph.add_conditional_edges(
        "process",
        route_by_query_type,
        {"handle_anomaly": "handle_anomaly", "handle_forecast": "handle_forecast"},
    )
    graph.add_edge("handle_anomaly", "emit_output")
    graph.add_edge("handle_forecast", "emit_output")
    graph.add_edge("emit_output", END)

    return graph.compile()
