"""Action planner — recommendations -> executable Action descriptors.

Strategy (deterministic, no extra LLM call):

1. Build an *entity catalog* from the normalized CSV records — every dimension
   value present in the data (regions, services, instance types, project tags),
   ranked by the cost attached to it.
2. For each recommendation, find the highest-cost catalog entity whose value is
   mentioned in the recommendation text. That entity's ``(table, column, value)``
   becomes the Action's target row.
3. Recommendations that mention no known entity become ``manual`` actions —
   surfaced for visibility but only acknowledged, never auto-applied.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from ..utils.logger import get_logger
from .models import Action

if TYPE_CHECKING:
    from ..config import PipelineConfig

log = get_logger("actions.planner")

# Columns that are measures or time axes, not actionable dimensions.
_NON_DIMENSION = {"cost", "date", "day", "timestamp", "month"}


def _build_catalog(records: dict[str, list[dict]]) -> list[dict]:
    """Return [{table, column, value, cost}] sorted by cost descending."""
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for table, rows in (records or {}).items():
        for row in rows:
            cost = row.get("cost")
            cost = float(cost) if isinstance(cost, (int, float)) else 0.0
            for col, val in row.items():
                if col in _NON_DIMENSION or not isinstance(val, str):
                    continue
                val = val.strip()
                if len(val) < 2:  # skip empties and single chars
                    continue
                totals[(table, col, val)] += cost

    catalog = [
        {"table": t, "column": c, "value": v, "cost": round(cost, 6)}
        for (t, c, v), cost in totals.items()
    ]
    catalog.sort(key=lambda e: e["cost"], reverse=True)
    return catalog


def _match_all_entities(text: str, catalog: list[dict]) -> list[dict]:
    """Every catalog entity mentioned in the recommendation text, cost-sorted.
    The first entry is the one the action targets; the full list becomes the
    action's ``citations`` — the evidence trail for why it was recommended."""
    low = text.lower()
    return [entity for entity in catalog if entity["value"].lower() in low]


def _match_entity(text: str, catalog: list[dict]) -> dict | None:
    matches = _match_all_entities(text, catalog)
    return matches[0] if matches else None


def _attach_preview(action: Action, cfg: "PipelineConfig") -> None:
    """Best-effort plan preview: which op, which instances, which calls, and a
    rough monthly saving — so the RE dashboard can show exactly what approving
    an action will do *before* anyone approves it. Never raises: a mock-AWS
    hiccup at planning time should not break the pipeline run.
    """
    if not action.executable or action.backend != "aws":
        return
    try:
        from .executor import AWSExecutor

        preview = AWSExecutor(cfg).preview(action)
        action.op = preview["op"]
        action.targets = preview["targets"]
        action.calls_preview = preview["calls"]
        action.estimated_savings = preview["estimated_savings"]
    except Exception as exc:  # pragma: no cover - defensive only
        log.warning("Plan preview failed for %s (%s); leaving it blank.", action.id, exc)


def plan_actions(
    run_id: str,
    combined: dict,
    records: dict[str, list[dict]],
    backend: str = "csv",
    cfg: "PipelineConfig | None" = None,
) -> list[Action]:
    """Build Action descriptors from the combined report's recommendations.

    ``cfg`` is optional and only used to compute a read-only plan preview
    (matched instances, the calls that will run, an estimated saving) for
    "aws" backend actions — pass it whenever it's available so the two-stage
    approval dashboard has real numbers to show the RE team.
    """
    catalog = _build_catalog(records)
    summary = combined.get("summary", {}) or {}
    recommendations = summary.get("recommendations", []) or []

    actions: list[Action] = []
    seen: set[str] = set()
    for rec in recommendations:
        if isinstance(rec, dict):
            title = str(rec.get("action") or rec.get("title") or "").strip()
            impact = str(rec.get("impact") or "medium").lower()
        else:
            title, impact = str(rec).strip(), "medium"
        if not title:
            continue

        matches = _match_all_entities(title, catalog)
        entity = matches[0] if matches else None
        if entity:
            action = Action.for_row(
                run_id=run_id,
                title=title,
                table=entity["table"],
                match_column=entity["column"],
                match_value=entity["value"],
                set_fields={
                    "action_status": "applied",
                    "applied_recommendation": title,
                },
                impact=impact,
                backend=backend,
            )
        else:
            action = Action.manual(run_id=run_id, title=title, impact=impact)
        action.citations = matches[:20]

        if action.id in seen:
            continue
        seen.add(action.id)
        if cfg is not None:
            _attach_preview(action, cfg)
            if action.executable and getattr(cfg.capabilities, "assess_risk", True):
                from .risk import assess_risk

                assess_risk(action, cfg)
        actions.append(action)

    log.info(
        "Planned %s actions (%s executable) from %s recommendations",
        len(actions),
        sum(1 for a in actions if a.executable),
        len(recommendations),
    )
    return actions
