from __future__ import annotations

from langgraph.prebuilt import create_react_agent

from .providers import build_llm_client, load_config
from .rag import build_retriever_tool
from .tools import check_spend_threshold, get_service_cost

SYSTEM_PROMPT = (
    "You are a FinOps assistant that explains AWS cost line items. "
    "Use the get_service_cost and check_spend_threshold tools to look up real "
    "spend numbers before making claims about cost. Use the search_cost_docs "
    "tool to retrieve CSV column definitions and AWS pricing reference "
    "context before explaining what a value means or whether a cost looks "
    "typical. Always ground your final answer in the tool results you "
    "actually retrieved during this conversation, and cite the document "
    "source file name (shown in the retrieved context) whenever you use "
    "retrieved document content in your answer."
)


def build_cost_agent(client_name: str = "anthropic_claude"):
    """Build a ReAct agent wired with the cost-lookup/threshold tools and a
    RAG retriever tool over the CSV schemas and AWS pricing documents."""
    config = load_config()
    llm = build_llm_client(client_name, config)
    tools = [get_service_cost, check_spend_threshold, build_retriever_tool(config)]
    return create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
