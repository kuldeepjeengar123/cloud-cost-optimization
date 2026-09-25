from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from .schemas import CostAnalysisResult, IntentClassification, NormalizedCostRecord, ToolHandlerResult

SYSTEM_PROMPT = (
    "You are a cloud cost analysis assistant. Given a single AWS Cost Explorer "
    "line item as JSON, decide whether the spend looks anomalous relative to "
    "typical usage for that service, and produce a concise, business-friendly "
    "one-sentence summary. Respond only through the provided structured schema."
)

_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        ("human", "Cost line item:\n{cost_row_json}"),
    ]
)


def build_cost_analysis_chain(llm: BaseChatModel) -> Runnable:
    """Wire prompt | llm.with_structured_output(..., include_raw=True) into a
    single runnable chain. include_raw=True keeps the underlying AIMessage
    (and its token usage metadata) alongside the parsed result, so callers
    can report real prompt/completion token counts per call."""
    structured_llm = llm.with_structured_output(CostAnalysisResult, include_raw=True)
    return _PROMPT | structured_llm


NORMALISE_SYSTEM_PROMPT = (
    "You are a data normalisation assistant for AWS cost records. You receive a "
    "single raw JSON record with inconsistent, abbreviated, or missing field "
    "names and values, coming from an upstream pipeline stage. Clean it into "
    "the canonical schema: map the service name to a standard AWS service "
    "name (e.g. 'Amazon EC2', 'Amazon S3', 'Amazon RDS'), convert "
    "numeric-looking strings to real numbers, and leave a field null only if "
    "it is genuinely missing or null in the input. Respond only through the "
    "provided structured schema."
)

_NORMALISE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", NORMALISE_SYSTEM_PROMPT),
        ("human", "Raw record:\n{raw_json}"),
    ]
)


def build_normalise_chain(llm: BaseChatModel) -> Runnable:
    """Step 1 chain: prompt | llm.with_structured_output(NormalizedCostRecord).
    Turns a raw, messy upstream record into the canonical schema."""
    structured_llm = llm.with_structured_output(NormalizedCostRecord)
    return _NORMALISE_PROMPT | structured_llm


INTENT_SYSTEM_PROMPT = (
    "You classify a user's request about an AWS cost record into exactly "
    "one of four fixed categories: cost_anomaly (the request asks why "
    "spend looks unusual), budget_forecast (the request asks about future "
    "spend), optimisation_recommendation (the request asks how to reduce "
    "cost), or usage_report (the request just wants a plain summary). "
    "Respond only through the provided structured schema."
)

_INTENT_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", INTENT_SYSTEM_PROMPT),
        ("human", "Record:\n{record_json}\n\nRequest: {request}"),
    ]
)


def build_intent_chain(llm: BaseChatModel) -> Runnable:
    """Week 5 chain: classifies a request into one of the four fixed intents."""
    structured_llm = llm.with_structured_output(IntentClassification)
    return _INTENT_PROMPT | structured_llm


_TOOL_HANDLER_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "You are a cloud cost assistant. {task_instruction} "
                   "Use only the record, its tags, and the retrieved pricing "
                   "context provided below. Respond only through the "
                   "provided structured schema."),
        ("human", "Record:\n{record_json}\n\nTags:\n{tags_json}\n\nPricing context:\n{pricing_json}"),
    ]
)


def build_tool_handler_chain(llm: BaseChatModel) -> Runnable:
    """Week 5 chain shared by all four tool handlers: prompt |
    llm.with_structured_output(ToolHandlerResult). Each handler supplies its
    own {task_instruction} at invoke time, so one chain definition covers
    all four intents."""
    structured_llm = llm.with_structured_output(ToolHandlerResult)
    return _TOOL_HANDLER_PROMPT | structured_llm
