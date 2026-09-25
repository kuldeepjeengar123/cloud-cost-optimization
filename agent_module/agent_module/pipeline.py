from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from .cache import SemanticCache
from .chains import build_normalise_chain
from .providers import build_embeddings_client, build_llm_client, load_config
from .rag import build_vectorstore
from .retry import invoke_with_retries

_PROJECT_ROOT = Path(__file__).parent.parent
_BUSINESS_RULES_PATH = _PROJECT_ROOT / "sample_data" / "business_rules.json"
_STATE_DIR = _PROJECT_ROOT / "state"
_SEMANTIC_CACHE_PATH = _STATE_DIR / "semantic_cache.json"


class PipelineState(TypedDict):
    """Shared state for Steps 1 and 2. raw_record comes in from the upstream
    module (M1); each later field is filled in as the record moves through
    the graph."""

    raw_record: Dict[str, Any]
    normalized_record: Optional[Dict[str, Any]]
    cache_hit: Optional[bool]
    normalise_latency_seconds: Optional[float]
    business_tags: Optional[Dict[str, Any]]
    pricing_context: Optional[List[Dict[str, str]]]


def load_business_rules() -> Dict[str, Any]:
    with _BUSINESS_RULES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _cache_key(raw_record: Dict[str, Any]) -> str:
    """A stable text representation of the raw record, used both as the
    thing we embed for the semantic cache and as a human-readable key."""
    return json.dumps(raw_record, sort_keys=True)


def apply_business_rules(normalized: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    """Step 2 business-rule logic: assign a cost centre and an environment
    tag, preferring an account-level mapping over a service-level one, and
    only inferring the environment if it was not already present."""
    cost_centre = (
        rules.get("cost_centre_by_account", {}).get(normalized.get("account_id"))
        or rules.get("cost_centre_by_service", {}).get(normalized.get("service"))
        or rules.get("default_cost_centre", "unassigned")
    )

    environment = normalized.get("environment")
    if not environment:
        resource_id = normalized.get("resource_id", "") or ""
        environment = rules.get("default_environment", "unknown")
        for prefix, env_name in rules.get("environment_by_resource_prefix", {}).items():
            if resource_id.startswith(prefix):
                environment = env_name
                break

    return {"cost_centre": cost_centre, "environment": environment}


def make_normalise_node(normalise_chain, cache: SemanticCache):
    """Factory for the Step 1 node, so it can be reused by other graphs
    (e.g. the Week 5/6 extension) without duplicating this logic."""

    def normalise_node(state: dict) -> dict:
        raw = state["raw_record"]
        key_text = _cache_key(raw)
        start = time.monotonic()

        cached, hit = invoke_with_retries(lambda: cache.get(key_text))
        if hit:
            normalized = cached
        else:
            result = invoke_with_retries(
                lambda: normalise_chain.invoke({"raw_json": json.dumps(raw, indent=2)})
            )
            normalized = result.model_dump()
            invoke_with_retries(lambda: cache.set(key_text, normalized))

        latency = time.monotonic() - start
        return {
            "normalized_record": normalized,
            "cache_hit": hit,
            "normalise_latency_seconds": latency,
        }

    return normalise_node


def make_enrich_node(rules: Dict[str, Any], vectorstore):
    """Factory for the Step 2 node, so it can be reused by other graphs."""

    def enrich_node(state: dict) -> dict:
        normalized = state["normalized_record"]
        tags = apply_business_rules(normalized, rules)

        query = f"{normalized.get('service')} pricing typical hourly rate"
        docs = invoke_with_retries(lambda: vectorstore.similarity_search(query, k=2))
        pricing_context = [
            {"source": doc.metadata.get("source", "unknown"), "text": doc.page_content}
            for doc in docs
        ]

        return {"business_tags": tags, "pricing_context": pricing_context}

    return enrich_node


def build_pipeline_graph(client_name: str = "anthropic_claude") -> CompiledStateGraph:
    """Build the Step 1 (normalise) + Step 2 (enrich) graph. The LLM,
    embeddings client, semantic cache, business rules, and FAISS vectorstore
    are all built once here and closed over by the node functions, so they
    persist across multiple graph.invoke()/graph.stream() calls. The
    semantic cache is also written to state/semantic_cache.json, so a
    record already seen stays cached across process restarts, not only
    within one run."""
    config = load_config()
    llm = build_llm_client(client_name, config)
    embeddings = build_embeddings_client(config)
    normalise_chain = build_normalise_chain(llm)
    cache = SemanticCache(embeddings, persist_path=_SEMANTIC_CACHE_PATH)
    rules = load_business_rules()
    vectorstore = build_vectorstore(config)

    normalise_node = make_normalise_node(normalise_chain, cache)
    enrich_node = make_enrich_node(rules, vectorstore)

    graph = StateGraph(PipelineState)
    graph.add_node("normalise", normalise_node)
    graph.add_node("enrich", enrich_node)
    graph.add_edge(START, "normalise")
    graph.add_edge("normalise", "enrich")
    graph.add_edge("enrich", END)

    return graph.compile()
