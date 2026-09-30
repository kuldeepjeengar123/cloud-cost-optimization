"""Action planner — recommendations -> executable Action descriptors.

Strategy (deterministic, no extra LLM call):

1. Build an *entity catalog* from the normalized cost records (fetched live
   from AWS Cost Explorer/CloudWatch — see src/inputs/) — every dimension
   value present in the data (regions, services, instance types, project tags),
   ranked by the cost attached to it.
2. For each recommendation, find the highest-cost catalog entity whose value is
   mentioned in the recommendation text. That entity's ``(table, column, value)``
   is attached as evidence (``citations``), but the recommendation itself is
   always surfaced as a ``manual`` action — informational only, acknowledged
   but never sent to AWS. The single exposed AWS write tool only supports
   changing an instance's type, so a cost-data-derived recommendation (stop,
   tag, terminate, budget, ...) has nothing safe to execute against; only the
   deterministic nano<->micro rightsizing swap below is ever AWS-executable.
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
    """Every catalog entity mentioned in the recommendation text, cost-sorted —
    becomes the action's ``citations``, the evidence trail for why it was
    recommended (cost-catalog recommendations are informational-only; see
    ``plan_actions`` below, so no entity is picked out as an apply target)."""
    low = text.lower()
    return [entity for entity in catalog if entity["value"].lower() in low]


# Deterministic EC2 rightsizing swap: nano is treated as undersized (recommend
# upsizing to micro) and micro as oversized for idle/low-traffic use (recommend
# downsizing to nano). This is independent of whatever the LLM's cost-driven
# recommendations say — it reads the live fleet directly so it always reflects
# the account's actual instance types, one action per real instance. Always
# attempted alongside the cost-catalog-matched recommendations in
# plan_actions() below (previously this ran only for a separate "EC2
# Rightsizing" target) — a missing/unreachable AWS credential just means it
# contributes zero actions, same as before.
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
            direction, reason, impact = "Upgrade", "undersized for steady load", "medium"
        else:
            direction, reason, impact = "Downgrade", "oversized for an idle/low-traffic workload", "low"
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


def _finalize_action(action: Action, cfg: "PipelineConfig | None", executor, seen: set[str]) -> Action | None:
    """Dedupe against ``seen``, then fill in the preview + risk assessment —
    applied uniformly to every action plan_actions() produces below, whether
    it's a cost-catalog match or a live-fleet-scanned nano/micro swap.
    Returns ``None`` for a duplicate."""
    if action.id in seen:
        return None
    seen.add(action.id)
    if cfg is not None:
        _attach_preview(action, cfg, executor)
        if action.executable and getattr(cfg.capabilities, "assess_risk", True):
            from .risk import assess_risk

            assess_risk(action, cfg, executor)
    return action


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
    """Build Action descriptors from the combined report's recommendations,
    plus — always — the deterministic live-fleet nano/micro rightsizing swap
    (see ``_plan_nano_micro_swap_actions``).

    Every cost-catalog-matched recommendation is surfaced as a non-executable
    ``manual`` action: the only AWS write path this app exposes is resizing
    an instance's type, so a recommendation about stopping, tagging,
    terminating, or budgets has nothing it can safely execute — it is shown
    for visibility and acknowledged, never applied. ``backend`` is kept for
    callers/tests that still pass it, but no longer changes executability.
    The nano/micro rightsizing swap actions are the sole exception: they are
    always ``backend="aws"`` and remain the only executable actions this
    planner produces.

    ``cfg`` is optional and only used to compute a read-only plan preview
    (matched instances, the calls that will run, an estimated saving) for
    "aws" backend actions, and to run the fleet scan itself — pass it
    whenever it's available so the two-stage approval dashboard has real
    numbers to show the RE team. Without AWS credentials configured, the
    fleet scan below simply contributes zero actions (see
    ``_plan_nano_micro_swap_actions``'s own try/except) rather than failing
    the whole run.
    """
    catalog = _build_catalog(records)
    summary = combined.get("summary", {}) or {}
    recommendations = summary.get("recommendations", []) or []

    # One executor for the whole planning pass, so the fleet is read once and
    # every preview/risk check below shares that snapshot — see
    # AWSExecutor.inventory(). Built whenever cfg is available, regardless of
    # this run's own cost-catalog ``backend``: it's reused below for the
    # always-on nano/micro fleet scan, and (only when an action's own
    # ``backend == "aws"``) for that action's preview/risk check.
    executor = None
    if cfg is not None:
        from .executor import AWSExecutor

        executor = AWSExecutor(cfg)

    actions: list[Action] = []
    seen: set[str] = set()

    def _collect(action: Action) -> None:
        finalized = _finalize_action(action, cfg, executor, seen)
        if finalized is not None:
            actions.append(finalized)

    for rec in recommendations:
        if isinstance(rec, dict):
            title = str(rec.get("action") or rec.get("title") or "").strip()
            impact = str(rec.get("impact") or "medium").lower()
        else:
            title, impact = str(rec).strip(), "medium"
        if not title:
            continue

        matches = _match_all_entities(title, catalog)
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
