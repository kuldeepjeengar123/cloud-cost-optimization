"""Guardrail: assess the blast radius of an executable action before it can
be approved.

The deterministic core mirrors the risk heuristic the web dashboard already
shows users (``finops_approval_prototype.html``'s ``riskFor()``/``queueCard()``
risk-note), so what gets enforced server-side matches what the UI has been
telling people all along. An optional LLM call turns that heuristic into a
one-line, situation-specific explanation for the RE team; if the call fails
or no API key is configured, a static reason (same wording as the UI) is used
instead, so the guardrail itself never depends on the LLM being available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm.client import LLMClient
from ..utils.logger import get_logger
from .models import Action

if TYPE_CHECKING:
    from ..config import PipelineConfig

log = get_logger("actions.risk")

_STATIC_REASONS = {
    "high": "Production resource behind live traffic. Confirm a maintenance window and "
            "that the remaining capacity can carry the load alone.",
    "medium": "Brief downtime on a non-production resource. Low impact expected, but "
              "confirm the change window.",
    "low": "Development-tagged or non-serving resource. No customer-facing impact expected.",
}


def _matched_instances(action: Action, cfg: "PipelineConfig", executor=None) -> list[dict]:
    if action.backend != "aws" or not action.executable:
        return []
    try:
        if executor is None:
            from .executor import AWSExecutor

            executor = AWSExecutor(cfg)
        return executor.matched_instances(action)
    except Exception as exc:  # pragma: no cover - defensive only
        log.warning("Could not resolve instances for risk check on %s (%s)", action.id, exc)
        return []


def _deterministic_level(action: Action, instances: list[dict]) -> str:
    if not action.executable:
        return "low"
    touches_prod = any(
        next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Env"), "").lower() == "prod"
        for inst in instances
    )
    if touches_prod:
        return "high"
    if action.op in ("resize", "stop", "terminate"):
        return "medium"
    return "low"


def assess_risk(action: Action, cfg: "PipelineConfig", executor=None) -> None:
    """Set ``action.risk_level``/``action.risk_reason`` in place.

    Never raises: a guardrail that can crash the run is worse than one that's
    merely conservative. Callers should invoke this after the action's
    ``op``/``targets`` preview has been filled in (see ``planner._attach_preview``).
    Pass the same ``executor`` used for the preview to reuse its one fleet
    read instead of paying for another per action.
    """
    instances = _matched_instances(action, cfg, executor)
    level = _deterministic_level(action, instances)
    action.risk_level = level
    action.risk_reason = _STATIC_REASONS[level]

    if not cfg.llm.api_key and not cfg.llm.api_key_fallback:
        return
    try:
        client = LLMClient(cfg.llm)
        result = client.complete_json(
            system=(
                "You write a single, concrete sentence explaining the operational "
                "blast radius of a proposed AWS change, for a Reliability Engineer "
                'who must approve or decline it. Return JSON: {"reason": str}.'
            ),
            user=(
                f"Recommendation: {action.title}\n"
                f"Operation: {action.op or 'unknown'}\n"
                f"Risk level already determined: {level}\n"
                f"Affected instance ids: {[i.get('InstanceId') for i in instances] or 'none resolved'}\n"
                f"Tags on affected instances: {[i.get('Tags') for i in instances] or 'n/a'}"
            ),
            model=cfg.llm.model_analysis,
        )
        reason = (result or {}).get("reason")
        if isinstance(reason, str) and reason.strip():
            action.risk_reason = reason.strip()
    except Exception as exc:  # pragma: no cover - defensive only
        log.warning("Risk narrative LLM call failed for %s (%s); using static reason.", action.id, exc)
