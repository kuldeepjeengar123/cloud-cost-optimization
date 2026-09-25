"""Action descriptor — the machine-readable form of a recommendation.

An ``Action`` is what the "Apply" button in Teams (or the web UI) acts on. It
references a concrete CSV row by ``(table, match_column, match_value)`` and the
fields to write. Generic recommendations that don't map to a row become
``executable=False`` "manual" actions that are still surfaced but only
acknowledged, never auto-applied.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_DISMISSED = "dismissed"
STATUS_ACKNOWLEDGED = "acknowledged"  # manual actions with no executable target

# Second approval stage (employee raises -> RE team decides). An action only
# ever reaches STATUS_APPLIED via STATUS_PENDING_RE now for actions that were
# raised through the two-stage flow; the single-stage Teams "Apply" link still
# goes STATUS_PENDING -> STATUS_APPLIED directly.
STATUS_PENDING_RE = "pending_re"
STATUS_DECLINED = "declined"

# Batch commit gate: the RE team's approve/decline on a queued request only
# *stages* an intent now. Nothing reaches AWS (or a CSV write) until every
# pending_re request in the queue has been staged one way or the other and
# the RE team explicitly commits the whole batch — see actions.approval.
# commit_batch(). staged_decline finalizes to STATUS_DECLINED on commit
# without ever touching an executor; staged_approve finalizes to
# STATUS_APPLIED/STATUS_ACKNOWLEDGED (or is left staged again on failure, so
# a failed item can be retried rather than silently lost).
STATUS_STAGED_APPROVE = "staged_approve"
STATUS_STAGED_DECLINE = "staged_decline"


def _make_id(*parts: Optional[str]) -> str:
    seed = "|".join("" if p is None else str(p) for p in parts)
    return "act_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


@dataclass
class Action:
    """A single applyable recommendation."""

    id: str
    run_id: str
    title: str                      # the recommendation text, shown to the user
    impact: str = "medium"          # low | medium | high

    # Concrete target (None -> manual / non-executable)
    table: Optional[str] = None         # CSV stem, e.g. "region_cost"
    match_column: Optional[str] = None  # normalized column, e.g. "region"
    match_value: Optional[str] = None   # value to match, e.g. "eu-north-1"
    set_fields: dict[str, Any] = field(default_factory=dict)  # written on apply

    executable: bool = False
    backend: str = "csv"            # which executor will run it (csv | aws)
    status: str = STATUS_PENDING
    applied_at: Optional[str] = None
    result_note: Optional[str] = None

    # --- plan preview, filled in by the planner/executor before any apply ---
    op: str = ""                              # inferred operation, e.g. "resize"
    targets: list[str] = field(default_factory=list)       # matched instance ids (aws backend)
    calls_preview: list[str] = field(default_factory=list)  # human-readable calls that WILL run
    estimated_savings: float = 0.0            # best-effort $/month estimate, 0 if unknown

    # --- two-stage approval workflow (employee raises, RE team decides) ---
    raised_by: Optional[str] = None
    raised_at: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decline_reason: Optional[str] = None

    # --- batch commit gate: staged intent, filled in before a commit ---
    staged_by: Optional[str] = None
    staged_at: Optional[str] = None
    commit_batch_id: Optional[str] = None
    committed_at: Optional[str] = None

    # --- guardrail: blast-radius assessment, filled in by planner.assess_risk ---
    risk_level: str = "unknown"     # low | medium | high
    risk_reason: str = ""

    # --- evidence trail: every catalog entity the recommendation text matched,
    # not just the single highest-cost one used to target the action ---
    citations: list[dict] = field(default_factory=list)

    @classmethod
    def manual(cls, run_id: str, title: str, impact: str = "medium") -> "Action":
        # No concrete target to key on, so the id stays tied to this run's
        # exact wording. ActionStore.upsert_many() treats any never-decided
        # ("pending") action whose id isn't in the latest run's batch as
        # stale and drops it, so old runs' manual suggestions get replaced
        # by the new run's rather than piling up alongside them forever.
        return cls(
            id=_make_id(run_id, title),
            run_id=run_id,
            title=title,
            impact=impact,
            executable=False,
        )

    @classmethod
    def for_row(
        cls,
        run_id: str,
        title: str,
        table: str,
        match_column: str,
        match_value: str,
        set_fields: dict[str, Any],
        impact: str = "medium",
        backend: str = "csv",
    ) -> "Action":
        # Keyed on the target row only (not run_id or title): the same
        # underlying recommendation ("rightsize this instance", "cap this
        # service's spend") gets the same id every run, so a later run
        # refreshes it in place instead of cloning it, and a decision made
        # on it (raised/applied/declined) survives future runs that mention
        # the same target. See ActionStore.upsert_many().
        return cls(
            id=_make_id(table, match_column, match_value),
            run_id=run_id,
            title=title,
            impact=impact,
            table=table,
            match_column=match_column,
            match_value=match_value,
            set_fields=set_fields,
            executable=True,
            backend=backend,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Action":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})
