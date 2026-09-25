from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from .cache import SemanticCache
from .chains import build_intent_chain, build_normalise_chain, build_tool_handler_chain
from .pipeline import apply_business_rules, load_business_rules, make_enrich_node, make_normalise_node
from .providers import build_embeddings_client, build_llm_client, load_config
from .rag import build_vectorstore
from .retry import invoke_with_retries

_PROJECT_ROOT = Path(__file__).parent.parent
_STATE_DIR = _PROJECT_ROOT / "state"
_SEMANTIC_CACHE_PATH = _STATE_DIR / "semantic_cache.json"
_CHECKPOINT_DB_PATH = _STATE_DIR / "checkpoints.sqlite"

# Week 5: one task instruction per fixed intent, all sharing the same
# ToolHandlerResult output schema (see chains.build_tool_handler_chain).
TOOL_TASK_INSTRUCTIONS: Dict[str, str] = {
    "cost_anomaly": (
        "Decide whether this record's spend looks anomalous relative to "
        "typical usage for this service, and explain why."
    ),
    "budget_forecast": (
        "Project what this record's spend is likely to look like next "
        "billing period, and explain your reasoning."
    ),
    "optimisation_recommendation": (
        "Recommend one specific, concrete action to reduce this record's "
        "cost, and explain why it would help."
    ),
    "usage_report": (
        "Summarize this record's service, spend, and usage in one or two "
        "plain sentences suitable for a report."
    ),
}

# Maps each fixed intent to the graph node that handles it. Reused both for
# the initial routing after detect_intent, and for routing a retry back to
# the same tool node after self_correct.
INTENT_TO_NODE: Dict[str, str] = {
    "cost_anomaly": "run_cost_anomaly",
    "budget_forecast": "run_budget_forecast",
    "optimisation_recommendation": "run_optimisation_recommendation",
    "usage_report": "run_usage_report",
}

MAX_RETRIES = 3
FALLBACK_FROM_RETRY = 2  # "on retry 2, switch to a different LLM model"
CONFIDENCE_THRESHOLD = 0.5

# Week 7: the model router. Each task is assigned a starting tier — "cheap"
# for simple classification/summarisation, "capable" for real analysis —
# following the curriculum's own split (Haiku/GPT-3.5 for simple
# classification, Claude Sonnet/GPT-4o for deep analysis). This is a
# separate concern from Week 6's retry fallback: the router picks the
# *starting* model for a task; Week 6's "switch model on retry 2" still
# applies on top, switching to the *other* tier if the starting one keeps
# failing.
MODEL_TIER_BY_TASK: Dict[str, str] = {
    "detect_intent": "cheap",
    "cost_anomaly": "capable",
    "budget_forecast": "capable",
    "optimisation_recommendation": "capable",
    "usage_report": "cheap",
}


def _other_tier(tier: str) -> str:
    return "cheap" if tier == "capable" else "capable"


class IntentPipelineState(TypedDict):
    """Extends Week 4's record-shaped state with the Week 5/6 fields: the
    accompanying request, the detected intent, the chosen tool's result, a
    running log of node names visited, and the retry/self-correct bookkeeping."""

    raw_record: Dict[str, Any]
    request: str

    normalized_record: Optional[Dict[str, Any]]
    cache_hit: Optional[bool]
    normalise_latency_seconds: Optional[float]
    business_tags: Optional[Dict[str, Any]]
    pricing_context: Optional[List[Dict[str, str]]]

    intent: Optional[str]
    tool_result: Optional[Dict[str, Any]]
    path: List[str]

    retry_count: int
    issue_found: Optional[bool]
    issue_reason: Optional[str]
    human_decision: Optional[Any]


def _with_path(node_name: str, node_fn):
    """Wrap a Week 4 node factory's function so it also appends its name to
    state['path'], without changing the Week 4 graph it was built for."""

    def wrapped(state: dict) -> dict:
        update = dict(node_fn(state))
        path = list(state.get("path") or [])
        path.append(node_name)
        update["path"] = path
        return update

    return wrapped


def make_detect_intent_node(intent_chain):
    def detect_intent_node(state: IntentPipelineState) -> dict:
        result = invoke_with_retries(
            lambda: intent_chain.invoke(
                {
                    "record_json": json.dumps(state["normalized_record"], indent=2),
                    "request": state.get("request", ""),
                }
            )
        )
        path = list(state.get("path") or [])
        path.append("detect_intent")
        return {"intent": result.intent, "path": path, "retry_count": 0}

    return detect_intent_node


def make_tool_node(intent_name: str, tool_chains_by_tier: Dict[str, Any]):
    """One node per intent. The Week 7 router picks the *starting* tier for
    this task (MODEL_TIER_BY_TASK); Week 6's fallback rule still applies on
    top, switching to the *other* tier once retry_count has reached
    FALLBACK_FROM_RETRY (i.e. 'on retry 2, switch to a different LLM model')."""

    task_instruction = TOOL_TASK_INSTRUCTIONS[intent_name]
    node_name = INTENT_TO_NODE[intent_name]
    starting_tier = MODEL_TIER_BY_TASK.get(intent_name, "capable")

    def tool_node(state: IntentPipelineState) -> dict:
        retry_count = state.get("retry_count") or 0
        use_fallback = retry_count >= FALLBACK_FROM_RETRY
        tier = _other_tier(starting_tier) if use_fallback else starting_tier
        chain = tool_chains_by_tier[tier]

        result = invoke_with_retries(
            lambda: chain.invoke(
                {
                    "task_instruction": task_instruction,
                    "record_json": json.dumps(state["normalized_record"], indent=2),
                    "tags_json": json.dumps(state.get("business_tags"), indent=2),
                    "pricing_json": json.dumps(state.get("pricing_context"), indent=2),
                }
            )
        )
        tool_result = result.model_dump()

        path = list(state.get("path") or [])
        path.append(f"{node_name} (model={tier}, attempt={retry_count + 1})")

        return {"tool_result": tool_result, "path": path}

    return tool_node


def make_self_correct_node():
    def self_correct_node(state: IntentPipelineState) -> dict:
        retry_count = state.get("retry_count") or 0
        result = state.get("tool_result") or {}

        finding = result.get("finding")
        supporting_fact = result.get("supporting_fact")
        confidence = result.get("confidence")
        if not finding or not supporting_fact or confidence is None:
            issue_found = True
            issue_reason = "Missing a required field in the tool result (hallucinated or incomplete)."
        else:
            issue_found = False
            issue_reason = None

        new_retry_count = retry_count + 1 if issue_found else retry_count

        path = list(state.get("path") or [])
        path.append("self_correct")

        return {
            "issue_found": issue_found,
            "issue_reason": issue_reason,
            "retry_count": new_retry_count,
            "path": path,
        }

    return self_correct_node


def route_after_self_correct(state: IntentPipelineState) -> str:
    if state.get("issue_found") and (state.get("retry_count") or 0) <= MAX_RETRIES:
        return state["intent"]  # retry the same tool node

    confidence = (state.get("tool_result") or {}).get("confidence")
    if confidence is not None and confidence < CONFIDENCE_THRESHOLD:
        return "human_interrupt"
    return "done"


def make_human_interrupt_node():
    def human_interrupt_node(state: IntentPipelineState) -> dict:
        decision = interrupt(
            {
                "reason": "Confidence below threshold after tool run.",
                "note": "Local stand-in for M4's real human-in-the-loop signal — M4 does not exist in this project.",
                "intent": state.get("intent"),
                "tool_result": state.get("tool_result"),
            }
        )
        path = list(state.get("path") or [])
        path.append("human_interrupt")
        return {"human_decision": decision, "path": path}

    return human_interrupt_node


@contextmanager
def build_intent_pipeline_graph(
    capable_client_name: str = "anthropic_claude",
    cheap_client_name: str = "openai_gpt",
) -> Iterator[CompiledStateGraph]:
    """Week 4 (normalise, enrich) extended with Week 5 (detect_intent, four
    tool handlers), Week 6 (self_correct retry loop with model fallback,
    human_interrupt as a local stand-in for M4), and Week 7 (a model router
    that starts each task on a cheap or capable model based on task
    complexity — see MODEL_TIER_BY_TASK).

    Use as: `with build_intent_pipeline_graph() as graph: ...`

    The checkpointer is a SqliteSaver writing to state/checkpoints.sqlite,
    not an in-memory MemorySaver — so a paused human_interrupt genuinely
    survives a process restart, not just a pause within one run. It is a
    context manager because SqliteSaver's connection must stay open for as
    long as the graph is used; the `with` block here keeps that connection
    open for the caller's whole session."""
    config = load_config()
    capable_llm = build_llm_client(capable_client_name, config)
    cheap_llm = build_llm_client(cheap_client_name, config)
    embeddings = build_embeddings_client(config)

    # normalise is not part of the Week 7 router (the curriculum's router
    # example covers classification and analysis tasks specifically) — it
    # keeps using the capable model, as it always has.
    normalise_chain = build_normalise_chain(capable_llm)
    cache = SemanticCache(embeddings, persist_path=_SEMANTIC_CACHE_PATH)
    rules = load_business_rules()
    vectorstore = build_vectorstore(config)

    normalise_node = _with_path("normalise", make_normalise_node(normalise_chain, cache))
    enrich_node = _with_path("enrich", make_enrich_node(rules, vectorstore))

    # detect_intent is simple classification -> routed to the cheap model.
    intent_chain = build_intent_chain(cheap_llm)
    detect_intent_node = make_detect_intent_node(intent_chain)

    tool_chains_by_tier = {
        "cheap": build_tool_handler_chain(cheap_llm),
        "capable": build_tool_handler_chain(capable_llm),
    }

    self_correct_node = make_self_correct_node()
    human_interrupt_node = make_human_interrupt_node()

    graph = StateGraph(IntentPipelineState)
    graph.add_node("normalise", normalise_node)
    graph.add_node("enrich", enrich_node)
    graph.add_node("detect_intent", detect_intent_node)
    for intent_name in INTENT_TO_NODE:
        graph.add_node(
            INTENT_TO_NODE[intent_name],
            make_tool_node(intent_name, tool_chains_by_tier),
        )
    graph.add_node("self_correct", self_correct_node)
    graph.add_node("human_interrupt", human_interrupt_node)

    graph.add_edge(START, "normalise")
    graph.add_edge("normalise", "enrich")
    graph.add_edge("enrich", "detect_intent")
    graph.add_conditional_edges("detect_intent", lambda s: s["intent"], INTENT_TO_NODE)
    for node_name in INTENT_TO_NODE.values():
        graph.add_edge(node_name, "self_correct")
    graph.add_conditional_edges(
        "self_correct",
        route_after_self_correct,
        {**INTENT_TO_NODE, "human_interrupt": "human_interrupt", "done": END},
    )
    graph.add_edge("human_interrupt", END)

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(_CHECKPOINT_DB_PATH)) as checkpointer:
        yield graph.compile(checkpointer=checkpointer)
