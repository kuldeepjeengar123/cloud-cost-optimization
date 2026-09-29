"""Parallel agent — Budget Forecast (capable tier).

One of four agents that replaced the old, single "Step 3.2: Analysis"
LLM call (see orchestrator.py). This one narrates the deterministic
cost-trend forecast already computed (``context["forecast"]``, see
``capabilities/forecasting.py``) into trends and benchmark commentary — the
forecast's own numbers are never recalculated here, only explained. Reads
only ``context``, never another agent's output, so it can run fully in
parallel with the other three.
"""

from __future__ import annotations

import json

from ..config import PipelineConfig
from ..llm.client import LLMClient
from ..prompts import load_prompt
from ..utils.logger import get_logger

log = get_logger("pipeline.run_budget_forecast")

SYSTEM_PROMPT = load_prompt("run_budget_forecast")


def _prompt(context: dict, user_query: str) -> str:
    snapshot = {
        "user_query": user_query,
        "business_metadata": context.get("business_metadata", {}),
        "cost_forecast": context.get("forecast") or None,
    }
    return (
        "Precomputed snapshot:\n"
        + json.dumps(snapshot, default=str, indent=2)
        + "\n\ncost_forecast (if present) is a deterministic trend projection — if its 'flag' is "
        "true, call out the trend as a trend or benchmark entry; if it's absent or 'flag' is "
        "false, note that the spend trend looks stable."
        "\n\nReturn JSON: {\n"
        '  "trends": [{"observation": str, "metric": str}],\n'
        '  "benchmarks": [{"metric": str, "value": number, "threshold_note": str}]\n'
        "}"
    )


def run_budget_forecast(cfg: PipelineConfig, context: dict, llm: LLMClient) -> dict:
    """Never raises — see run_cost_anomaly's docstring for the contract."""
    log.info("Budget forecast agent")
    try:
        raw = llm.complete(
            system=SYSTEM_PROMPT,
            user=_prompt(context, cfg.user_query),
            model=cfg.llm.model_analysis,
            max_tokens=1200,
        )
        parsed = LLMClient.extract_json(raw)
        if isinstance(parsed, dict):
            trends = parsed.get("trends")
            benchmarks = parsed.get("benchmarks")
            return {
                "trends": trends if isinstance(trends, list) else [],
                "benchmarks": benchmarks if isinstance(benchmarks, list) else [],
            }
        log.warning("Budget forecast agent did not return JSON; returning no narrative.")
    except Exception as exc:
        log.warning("Budget forecast agent failed (%s); returning no narrative.", exc)
    return {"trends": [], "benchmarks": []}
