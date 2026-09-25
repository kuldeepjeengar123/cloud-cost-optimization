"""Two-stage approval workflow — employee raises, RE team decides, RE commits.

This sits alongside the single-stage Teams "Apply" flow in ``review.py`` /
``executor.py`` without touching it: a Teams card still goes
``pending -> applied`` in one click. This module adds the gates the web
dashboard needs:

    pending --raise--> pending_re --stage(approve)--> staged_approve --\\
                              |                                         |
                              \\--stage(decline)--> staged_decline --commit_batch--> declined
                                                                         |
                                                          commit_batch --/--> applied/acknowledged
                                                                              (CSV written, or AWS called)

Staging a decision never touches AWS or a CSV — it only records RE intent.
``commit_batch()`` is the *only* function in this codebase that ever calls an
executor for a two-stage action, and it refuses to run while any request is
still sitting in ``pending_re`` (see its docstring): the RE team must have
triaged the entire queue — approved or declined every item — before anything
actually changes. A staged item can also be pulled back to ``pending_re`` with
``unstage()`` any time before the batch commits.

Every transition is written to the ``DecisionLogStore`` so both dashboards can
show *why* something happened, not just its current state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import PipelineConfig
from ..utils.logger import get_logger
from .decision_log import DecisionLogStore
from .executor import ApplyResult, get_executor
from .models import (
    STATUS_DECLINED,
    STATUS_DISMISSED,
    STATUS_PENDING,
    STATUS_PENDING_RE,
    STATUS_STAGED_APPROVE,
    STATUS_STAGED_DECLINE,
    Action,
)
from .store import ActionStore

log = get_logger("actions.approval")


class ApprovalError(ValueError):
    """Raised when a transition is attempted from an invalid state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(decision_log: DecisionLogStore, action: Action, decision: str, actor: str, note: str) -> None:
    decision_log.add({
        "at": _now(),
        "action_id": action.id,
        "title": action.title,
        "decision": decision,
        "actor": actor,
        "estimated_savings": action.estimated_savings,
        "note": note,
    })


def raise_for_review(store: ActionStore, decision_log: DecisionLogStore, action: Action, raised_by: str) -> Action:
    """Employee accepts an open recommendation and sends it to the RE queue."""
    if not action.executable:
        raise ApprovalError("Manual recommendations have nothing to raise for approval; acknowledge instead.")
    if action.status != STATUS_PENDING:
        raise ApprovalError(f"Cannot raise an action in status '{action.status}'.")
    action.status = STATUS_PENDING_RE
    action.raised_by = raised_by
    action.raised_at = _now()
    store.save(action)
    _log(decision_log, action, "raised", raised_by, "Sent to the RE team queue.")
    return action


def withdraw(store: ActionStore, decision_log: DecisionLogStore, action: Action, actor: str) -> Action:
    """Employee pulls a request back before the RE team has decided."""
    if action.status != STATUS_PENDING_RE:
        raise ApprovalError(f"Cannot withdraw an action in status '{action.status}'.")
    action.status = STATUS_PENDING
    action.raised_by = None
    action.raised_at = None
    store.save(action)
    _log(decision_log, action, "withdrawn", actor, "Removed from the queue before a decision.")
    return action


def reopen(store: ActionStore, decision_log: DecisionLogStore, action: Action, actor: str) -> Action:
    """Bring a declined or self-dismissed action back to the open list."""
    if action.status not in (STATUS_DECLINED, STATUS_DISMISSED):
        raise ApprovalError(f"Cannot reopen an action in status '{action.status}'.")
    action.status = STATUS_PENDING
    action.raised_by = None
    action.raised_at = None
    action.decided_by = None
    action.decided_at = None
    action.decline_reason = None
    action.result_note = None
    store.save(action)
    _log(decision_log, action, "reopened", actor, "Back in the open list.")
    return action


def stage_decision(
    store: ActionStore,
    decision_log: DecisionLogStore,
    action: Action,
    decision: str,
    staged_by: str,
    reason: str | None = None,
    override: bool = False,
) -> Action:
    """The RE team's approve/decline intent on a queued request.

    This only ever moves an action to ``staged_approve``/``staged_decline`` —
    it never calls an executor and never writes AWS or a CSV row. The actual
    change lands only when ``commit_batch()`` runs, and only once the whole
    queue has been triaged (see module docstring).

    A ``high`` ``risk_level`` (see ``actions.risk.assess_risk``) blocks staging
    an approval unless the caller explicitly passes ``override=True`` — the
    guardrail this adds on top of the two-stage queue. Checking it here rather
    than at commit time means the RE team is told immediately, while they are
    still looking at that specific request.
    """
    if action.status != STATUS_PENDING_RE:
        raise ApprovalError(f"Cannot stage a decision for an action in status '{action.status}'.")

    if decision == "approve":
        if action.risk_level == "high" and not override:
            raise ApprovalError(
                "High-risk action requires an explicit override. "
                f"{action.risk_reason or ''}".strip()
            )
        action.status = STATUS_STAGED_APPROVE
        action.staged_by = staged_by
        action.staged_at = _now()
        store.save(action)
        note = "Staged for approval" + (" (risk override)" if override and action.risk_level == "high" else "")
        _log(decision_log, action, "staged_approve", staged_by, note)
        return action

    if decision == "decline":
        if not (reason or "").strip():
            raise ApprovalError("A decline reason is required.")
        action.status = STATUS_STAGED_DECLINE
        action.decline_reason = reason.strip()
        action.staged_by = staged_by
        action.staged_at = _now()
        store.save(action)
        _log(decision_log, action, "staged_decline", staged_by, f"Staged to decline. Reason: {action.decline_reason}")
        return action

    raise ApprovalError(f"Unknown decision '{decision}'; expected 'approve' or 'decline'.")


def unstage(store: ActionStore, decision_log: DecisionLogStore, action: Action, actor: str) -> Action:
    """Pull a staged approve/decline back to ``pending_re``, before a commit."""
    if action.status not in (STATUS_STAGED_APPROVE, STATUS_STAGED_DECLINE):
        raise ApprovalError(f"Cannot unstage an action in status '{action.status}'.")
    action.status = STATUS_PENDING_RE
    action.staged_by = None
    action.staged_at = None
    action.decline_reason = None
    store.save(action)
    _log(decision_log, action, "unstaged", actor, "Back to the approval queue, awaiting a decision.")
    return action


def commit_status(store: ActionStore) -> dict:
    """Read-only readiness check the RE dashboard polls before offering the
    Commit button: how many requests are still undecided, how many are
    staged either way, and the total exposure if the batch runs now."""
    all_actions = store.all()
    undecided = [a for a in all_actions if a.status == STATUS_PENDING_RE]
    staged_approve = [a for a in all_actions if a.status == STATUS_STAGED_APPROVE]
    staged_decline = [a for a in all_actions if a.status == STATUS_STAGED_DECLINE]
    return {
        "undecided": len(undecided),
        "staged_approve": len(staged_approve),
        "staged_decline": len(staged_decline),
        "ready": len(undecided) == 0 and (staged_approve or staged_decline) != [],
        "estimated_savings": round(sum(a.estimated_savings or 0 for a in staged_approve), 2),
        "undecided_ids": [a.id for a in undecided],
    }


def commit_batch(cfg: PipelineConfig, store: ActionStore, decision_log: DecisionLogStore, decided_by: str) -> dict:
    """Apply every staged decision as one batch — the only path from a staged
    intent to a real AWS call or CSV write.

    Refuses outright while any request in the queue is still ``pending_re``:
    the RE team must decide (approve or decline) every open request first, so
    a partial review can never silently let some changes through while others
    are still being considered. ``staged_decline`` entries finalize to
    ``declined`` without ever reaching an executor.

    Before any executor runs, every AWS-backed ``staged_approve`` action has
    its live target state snapshotted (see ``rollback.snapshot_before``) and
    written to ``outputs/applied/<batch_id>/rollback.json``, so the whole
    batch can be undone with ``rollback.rollback_batch()``.

    A per-action failure (or a partial failure across an action's several
    targeted instances — see ``AWSExecutor.apply``) does **not** mark that
    action applied: it is left in ``staged_approve`` so the RE team can retry
    the commit later, while every other action in the batch still proceeds.
    """
    status = commit_status(store)
    if status["undecided"]:
        raise ApprovalError(
            f"{status['undecided']} request(s) still awaiting a decision; "
            "stage every item in the queue before committing."
        )
    staged = store.by_status(STATUS_STAGED_APPROVE, STATUS_STAGED_DECLINE)
    if not staged:
        raise ApprovalError("Nothing staged to commit.")

    from . import rollback as rollback_mod

    batch_id = f"batch_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"

    # Snapshot pre-mutation state for every approval candidate now (it has to
    # happen before that action's executor runs), but only *keep* the entries
    # for actions that actually succeed below — a failed/dry-run apply never
    # touched anything, so there is nothing for it to roll back.
    to_approve = [a for a in staged if a.status == STATUS_STAGED_APPROVE]
    snapshot_by_id = {a.id: rollback_mod.snapshot_before(a, cfg) for a in to_approve}

    results, applied, failed, declined = [], [], [], []
    for action in staged:
        if action.status == STATUS_STAGED_DECLINE:
            action.status = STATUS_DECLINED
            action.decided_by = decided_by
            action.decided_at = _now()
            action.commit_batch_id = batch_id
            action.committed_at = _now()
            store.save(action)
            note = f"Not applied. Reason: {action.decline_reason}"
            _log(decision_log, action, "declined", decided_by, note)
            declined.append(action)
            results.append({"id": action.id, "ok": True, "decision": "declined", "message": note})
            continue

        result = get_executor(cfg, backend=action.backend).apply(action)
        if not result.ok and result.action.status == STATUS_STAGED_APPROVE:
            # Left staged on purpose (see docstring): a failed apply must be
            # retryable, not silently recorded as if nothing happened.
            store.save(result.action)
            _log(decision_log, result.action, "commit_failed", decided_by,
                 f"{result.message} — left staged for retry.")
            failed.append(result.action)
            results.append({"id": action.id, "ok": False, "decision": "failed", "message": result.message})
            continue

        result.action.decided_by = decided_by
        result.action.decided_at = _now()
        result.action.commit_batch_id = batch_id
        result.action.committed_at = _now()
        store.save(result.action)
        _log(decision_log, result.action, "approved", decided_by, result.message)
        applied.append(result.action)
        results.append({"id": action.id, "ok": result.ok, "decision": "approved", "message": result.message})

    kept_snapshots = [snapshot_by_id.get(a.id) for a in applied]
    rollback_path = rollback_mod.write_batch_snapshot(cfg, batch_id, kept_snapshots)

    summary = {
        "batch_id": batch_id,
        "applied": len(applied),
        "declined": len(declined),
        "failed": len(failed),
        "rollback_available": rollback_path is not None,
        "results": results,
    }
    log.info("Committed batch %s: %s applied, %s declined, %s failed",
             batch_id, len(applied), len(declined), len(failed))
    return summary
