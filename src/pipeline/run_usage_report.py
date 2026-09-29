"""Parallel agent — Usage Report (cheap tier).

One of four agents that replaced the old, single "Step 3.2: Analysis"
LLM call (see orchestrator.py). Deliberately the "cheap tier" agent: a
factual usage summary needs far less reasoning than anomaly/forecast/
optimization analysis, so it's the one agent in this group that calls
``cfg.llm.model_cheap`` instead of ``model_analysis``. Reads only
``context``, never another agent's output, so it can run fully in parallel
with the other three.
"""

from __future__ import annotations

import json

from ..config import PipelineConfig
from ..llm.client import LLMClient
from ..prompts import load_prompt
from ..utils.logger import get_logger

log = get_logger("pipeline.run_usage_report")

SYSTEM_PROMPT = load_prompt("run_usage_report")


def _prompt(context: dict, user_query: str) -> str:
    snapshot = {
        "user_query": user_query,
        "business_metadata": context.get("business_metadata", {}),
        "correlations": {k: v[:5] for k, v in context.get("correlations", {}).items()},
    }
    return (
        "Precomputed snapshot:\n"
        + json.dumps(snapshot, default=str, indent=2)
        + "\n\nReturn JSON: {\n"
        '  "usage_summary": str,\n'
        '  "highlights": [str]\n'
        "}"
    )


def run_usage_report(cfg: PipelineConfig, context: dict, llm: LLMClient) -> dict:
    """Never raises — see run_cost_anomaly's docstring for the contract."""
    log.info("Usage report agent")
    try:
        raw = llm.complete(
            system=SYSTEM_PROMPT,
            user=_prompt(context, cfg.user_query),
            model=cfg.llm.model_cheap,
            max_tokens=800,
        )
        parsed = LLMClient.extract_json(raw)
        if isinstance(parsed, dict):
            highlights = parsed.get("highlights")
            return {
                "usage_summary": str(parsed.get("usage_summary") or ""),
                "highlights": highlights if isinstance(highlights, list) else [],
            }
        log.warning("Usage report agent did not return JSON; returning empty report.")
    except Exception as exc:
        log.warning("Usage report agent failed (%s); returning empty report.", exc)
    return {"usage_summary": "", "highlights": []}
