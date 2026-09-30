"""Parallel agent — Cost Anomaly (capable tier).

One of four agents that replaced the old, single "Step 3.2: Analysis"
LLM call (see orchestrator.py) with four concurrently-running, narrowly
scoped agents. This one narrates and prioritizes the anomalies already found
upstream in Step 2 (``context["anomaly_signals"]``) — an LLM call there
first, falling back to the deterministic day-over-day detector
(``capabilities/anomaly_detection.py``) only if that call fails; see
``step2_context_load.py``'s ``_llm_detect_anomalies``. Reads only
``context``, never another agent's output, so it can run fully in parallel
with the other three.
"""

from __future__ import annotations

import json

from ..config import PipelineConfig
from ..llm.client import LLMClient
from ..prompts import load_prompt
from ..utils.logger import get_logger

log = get_logger("pipeline.run_cost_anomaly")

SYSTEM_PROMPT = load_prompt("run_cost_anomaly")


def _prompt(context: dict, user_query: str) -> str:
    snapshot = {
        "user_query": user_query,
        "business_metadata": context.get("business_metadata", {}),
        "correlations": {k: v[:10] for k, v in context.get("correlations", {}).items()},
        "detected_anomalies": context.get("anomaly_signals", [])[:10],
    }
    return (
        "Precomputed snapshot:\n"
        + json.dumps(snapshot, default=str, indent=2)
        + "\n\ndetected_anomalies were found by a statistical day-over-day check, not by you — "
        "include the ones that matter in your own anomalies list (you may reword them), and add "
        "any further anomalies you notice. Some anomalies carry a 'drivers' list — other "
        "dimensions that coincided with the spike; mention the top one if it's informative."
        "\n\nReturn JSON: {\n"
        '  "anomalies": [{"finding": str, "severity": "low"|"medium"|"high", "evidence": str}]\n'
        "}"
    )


def _merge_detector_anomalies(anomalies: list[dict], detected: list[dict]) -> list[dict]:
    """Union the detector's findings into the LLM's anomalies list (by
    ``finding`` text) so a real spike still surfaces even if the LLM ignored
    it, dropped the field, or the call failed outright."""
    seen = {a.get("finding") for a in anomalies if isinstance(a, dict)}
    for finding in detected:
        if finding["finding"] not in seen:
            anomalies.append(finding)
            seen.add(finding["finding"])
    return anomalies


def run_cost_anomaly(cfg: PipelineConfig, context: dict, llm: LLMClient) -> dict:
    """Never raises: an agent that can crash the run is worse than one
    that's merely conservative — same contract every step in this pipeline
    follows."""
    log.info("Cost anomaly agent")
    detected = context.get("anomaly_signals", [])
    anomalies: list = []
    try:
        raw = llm.complete(
            system=SYSTEM_PROMPT,
            user=_prompt(context, cfg.user_query),
            model=cfg.llm.model_analysis,
            max_tokens=1800,
        )
        parsed = LLMClient.extract_json(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("anomalies"), list):
            anomalies = parsed["anomalies"]
        else:
            log.warning("Cost anomaly agent did not return JSON; using detector findings only.")
    except Exception as exc:
        log.warning("Cost anomaly agent failed (%s); using detector findings only.", exc)
    return {"anomalies": _merge_detector_anomalies(anomalies, detected)}
