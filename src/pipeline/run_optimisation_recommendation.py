"""Parallel agent — Optimisation Recommendation (capable tier).

One of four agents that replaced the old, single "Step 3.2: Analysis"
LLM call (see orchestrator.py). This one proposes concrete cost-optimization
recommendations from the same already-computed signals every other
replacement agent reads (anomalies, tag-governance findings, KPIs) — never
another agent's live output, since all four run concurrently.

Its output reaches the approval queue two ways: Step 3.3 (Summary) is asked
to fold these in, and — if that LLM call fails — its fallback appends them
directly (see ``step3_3_summary.py``'s ``_fallback_summary``), so a
recommendation found here never depends on a second LLM call succeeding to
reach a human reviewer.
"""

from __future__ import annotations

import json

from ..config import PipelineConfig
from ..llm.client import LLMClient
from ..prompts import load_prompt
from ..utils.logger import get_logger

log = get_logger("pipeline.run_optimisation_recommendation")

SYSTEM_PROMPT = load_prompt("run_optimisation_recommendation")


def _prompt(context: dict, kpis: dict, user_query: str) -> str:
    snapshot = {
        "user_query": user_query,
        "kpis": kpis,
        "business_metadata": context.get("business_metadata", {}),
        "detected_anomalies": context.get("anomaly_signals", [])[:10],
        "tag_governance_findings": context.get("tag_findings", [])[:10],
    }
    return (
        "Precomputed snapshot:\n"
        + json.dumps(snapshot, default=str, indent=2)
        + "\n\nTurn the anomalies and tag-governance findings that matter into concrete "
        "recommendations, plus anything else worth recommending from the KPIs."
        "\n\nReturn JSON: {\n"
        '  "recommendations": [{"action": str, "impact": "low"|"medium"|"high"}]\n'
        "}"
    )


def run_optimisation_recommendation(cfg: PipelineConfig, context: dict, kpis: dict, llm: LLMClient) -> dict:
    """Never raises — see run_cost_anomaly's docstring for the contract."""
    log.info("Optimisation recommendation agent")
    try:
        raw = llm.complete(
            system=SYSTEM_PROMPT,
            user=_prompt(context, kpis, cfg.user_query),
            model=cfg.llm.model_analysis,
            max_tokens=1500,
        )
        parsed = LLMClient.extract_json(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("recommendations"), list):
            return {"recommendations": parsed["recommendations"]}
        log.warning("Optimisation recommendation agent did not return JSON; returning none.")
    except Exception as exc:
        log.warning("Optimisation recommendation agent failed (%s); returning none.", exc)
    return {"recommendations": []}
