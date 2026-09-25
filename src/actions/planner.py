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

# Columns that are measures or time axes, not actionable dimensions. "year"
# in particular must be excluded like its day/month siblings: enrich_records()
# stamps it on every row from the date, so with a single-year dataset its
# catalog entry aggregates almost the *entire* account's cost under one value
# — any recommendation that merely mentions a date (e.g. "...spike on
# 2026-04-08") would otherwise get hijacked onto that meaningless "target"
# instead of the entity the recommendation is actually about.
_NON_DIMENSION = {"cost", "date", "day", "timestamp", "month", "year"}


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


def _select_target(text: str, matches: list[dict]) -> dict | None:
    """Pick which matched entity a recommendation's row-level change targets.

    ``matches`` is cost-sorted, so the top entry is normally "the" dimension
    a recommendation is about. But a recommendation that lists several
    candidates of the *same* dimension — e.g. "Replace t3.nano instances with
    t3.micro (or t3.small) or consider t3.medium or m5.large" — would
    otherwise grab whichever alternative happens to be the most heavily used
    elsewhere in the whole account, instead of the instance the
    recommendation is actually about. Within that top-ranked (table, column)
    group, prefer whichever value the text mentions first instead; groups of
    one (the overwhelming majority of recommendations) are unaffected.
    """
    if not matches:
        return None
    top_table, top_column = matches[0]["table"], matches[0]["column"]
    same_group = [m for m in matches if m["table"] == top_table and m["column"] == top_column]
    if len(same_group) == 1:
        return matches[0]
    low = text.lower()
    same_group.sort(key=lambda m: low.find(m["value"].lower()))
    return same_group[0]


def _match_entity(text: str, catalog: list[dict]) -> dict | None:
    return _select_target(text, _match_all_entities(text, catalog))


# Deterministic EC2 rightsizing swap: nano is treated as undersized (recommend
# upsizing to micro) and micro as oversized for idle/low-traffic use (recommend
# downsizing to nano). This is independent of whatever the LLM's CSV-driven
# recommendations say — it reads the live fleet directly so it always reflects
# the account's actual instance types, one action per real instance.
_NANO_MICRO_SWAP = {"nano": "micro", "micro": "nano"}


def _plan_nano_micro_swap_actions(run_id: str, cfg: "PipelineConfig", executor) -> list[Action]:
    from ..aws import pricing

    try:
        instances = executor.inventory()
    except Exception as exc:  # pragma: no cover - network/credential dependent
        log.warning("Nano/micro rightsizing scan skipped: could not read the live fleet (%s)", exc)
        return []

    actions: list[Action] = []
    for inst in instances:
        state = inst.get("State", {}).get("Name", "unknown")
        if state not in ("running", "stopped"):
            continue  # terminated/terminating instances aren't actionable
        itype = inst.get("InstanceType", "")
        family, _, size = itype.partition(".")
        target_size = _NANO_MICRO_SWAP.get(size)
        if not target_size:
            continue
        target_type = f"{family}.{target_size}"
        if target_type not in pricing.HOURLY_PRICE:
            continue

        instance_id = inst["InstanceId"]
        if size == "nano":
            direction, reason, impact = "Upsize", "undersized for steady load", "medium"
        else:
            direction, reason, impact = "Downsize", "oversized for an idle/low-traffic workload", "low"
        title = (f"{direction} {instance_id} from {itype} to {target_type} "
                 f"({reason}) — currently {state}")

        actions.append(Action.for_row(
            run_id=run_id,
            title=title,
            table="ec2_instance",
            match_column="instance_id",
            match_value=instance_id,
            set_fields={"action_status": "applied", "applied_recommendation": title},
            impact=impact,
            backend="aws",
        ))
    return actions


def _attach_preview(action: Action, cfg: "PipelineConfig", executor=None) -> None:
    """Best-effort plan preview: which op, which instances, which calls, and a
    rough monthly saving — so the RE dashboard can show exactly what approving
    an action will do *before* anyone approves it. Never raises: an AWS
    hiccup at planning time should not break the pipeline run.

    Pass an ``executor`` to reuse its one fleet read across every action in a
    planning pass; without one, this builds its own (and pays for its own read).
    """
    if not action.executable or action.backend != "aws":
        return
    try:
        if executor is None:
            from .executor import AWSExecutor

            executor = AWSExecutor(cfg)
        preview = executor.preview(action)
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

    # One executor for the whole planning pass, so the fleet is read once and
    # every preview/risk check below shares that snapshot — see
    # AWSExecutor.inventory().
    executor = None
    if cfg is not None and backend == "aws":
        from .executor import AWSExecutor

        executor = AWSExecutor(cfg)

    actions: list[Action] = []
    seen: set[str] = set()

    def _collect(action: Action) -> None:
        """Drop duplicates, then fill in the preview + risk assessment."""
        if action.id in seen:
            return
        seen.add(action.id)
        if cfg is not None:
            _attach_preview(action, cfg, executor)
            if action.executable and getattr(cfg.capabilities, "assess_risk", True):
                from .risk import assess_risk

                assess_risk(action, cfg, executor)
        actions.append(action)

    for rec in recommendations:
        if isinstance(rec, dict):
            title = str(rec.get("action") or rec.get("title") or "").strip()
            impact = str(rec.get("impact") or "medium").lower()
        else:
            title, impact = str(rec).strip(), "medium"
        if not title:
            continue

        matches = _match_all_entities(title, catalog)
        entity = _select_target(title, matches)
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

        _collect(action)

    if executor is not None:
        for action in _plan_nano_micro_swap_actions(run_id, cfg, executor):
            _collect(action)

    log.info(
        "Planned %s actions (%s executable) from %s recommendations",
        len(actions),
        sum(1 for a in actions if a.executable),
        len(recommendations),
    )
    return actions
