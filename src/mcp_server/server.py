"""AWS Cost & Ops MCP server — the one channel an AI agent has into this
project's AWS state.

Every read tool is open to any caller. Every tool that can change something
funnels through the exact same code path the web dashboard uses
(``src.actions.approval``): an agent can raise a recommendation and stage a
decision, but nothing reaches AWS or a CSV row except through
``commit_approved_changes``, which refuses to run while any request in the
queue is still undecided — same batch gate a human RE reviewer is held to.
There is deliberately no standalone "just call AWS" tool (not even for
budgets): every mutation is a recommendation that goes through raise -> stage
-> commit, so an agent can never take a shortcut a human reviewer can't.

RE-only tools additionally require ``re_token`` to match ``RE_TEAM_TOKEN``
from ``.env`` (see ``policy.require_re_role()``) once that's configured — the
same shared-secret gate the HTTP dashboard uses, not a second definition of
who counts as the RE team.

Every call, allowed or denied, is appended to ``outputs/mcp_audit.jsonl`` (see
``audit.py``) so agent-initiated activity is visible next to human activity
in the RE dashboard's log.

Run it:
    python mcp_server.py     # stdio transport, for Claude Code / Claude Desktop
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..actions import (
    ActionStore,
    ApprovalError,
    DecisionLogStore,
    commit_batch,
    commit_status,
    raise_for_review,
    rollback_batch,
    stage_decision,
    unstage,
    withdraw,
)
from ..actions.executor import AWSExecutor
from ..config import load_config
from ..utils.logger import get_logger
from . import audit
from .policy import NotAuthorized, require_re_role

log = get_logger("mcp_server")

CFG = load_config()
STORE = ActionStore(CFG.output_folder / "pending_actions.json")
DECISION_LOG = DecisionLogStore(CFG.output_folder / "decision_log.json")
AUDIT_PATH = CFG.output_folder / "mcp_audit.jsonl"

mcp = FastMCP(
    "aws-cost-ops",
    instructions=(
        "Tools for the AWS Cost & Ops Insights approval workflow. Every read is "
        "always safe to call. Every write funnels through the two-stage, "
        "batch-commit approval gate: raise_recommendation -> (RE-only) "
        "stage_recommendation_decision on every open request in the queue -> "
        "(RE-only) commit_approved_changes. Nothing reaches AWS or a CSV "
        "outside that path — there is no direct 'just call AWS' tool."
    ),
)


def _client():
    if CFG.aws_use_mock:
        from ..mock_aws.client import MockAWSClient, ensure_mock_server

        ensure_mock_server(CFG.aws_endpoint_url)
        return MockAWSClient(CFG.aws_endpoint_url)
    from ..aws.real_client import RealAWSClient

    return RealAWSClient(CFG)


def _log_ok(tool: str, arguments: dict, role: str, result) -> dict:
    audit.log_call(AUDIT_PATH, tool, arguments, role, True, result=result)
    return result


def _log_denied(tool: str, arguments: dict, role: str, error: str) -> dict:
    audit.log_call(AUDIT_PATH, tool, arguments, role, False, error=error)
    return {"ok": False, "error": error}


def _log_error(tool: str, arguments: dict, role: str, error: str) -> dict:
    audit.log_call(AUDIT_PATH, tool, arguments, role, True, error=error)
    return {"ok": False, "error": error}


# ------------------------------------------------------------------ reads --

@mcp.tool()
def list_recommendations(status: str | None = None) -> dict:
    """List planned recommendations, optionally filtered by status: pending,
    pending_re, staged_approve, staged_decline, applied, declined, dismissed,
    or acknowledged."""
    actions = STORE.all()
    if status:
        actions = [a for a in actions if a.status == status]
    return _log_ok("list_recommendations", {"status": status}, "any",
                   {"ok": True, "actions": [a.to_dict() for a in actions]})


@mcp.tool()
def get_recommendation(action_id: str) -> dict:
    """Full detail for one recommendation, including its evidence citations
    (the catalog entities its text matched)."""
    args = {"action_id": action_id}
    action = STORE.get(action_id)
    if not action:
        return _log_error("get_recommendation", args, "any", f"Unknown action id '{action_id}'")
    return _log_ok("get_recommendation", args, "any", {"ok": True, "action": action.to_dict()})


@mcp.tool()
def preview_action(action_id: str) -> dict:
    """Read-only: which AWS operation, which instances, which calls, and the
    estimated monthly saving approving this recommendation would produce.
    Never mutates anything — safe to call on any recommendation, decided or
    not."""
    args = {"action_id": action_id}
    action = STORE.get(action_id)
    if not action:
        return _log_error("preview_action", args, "any", f"Unknown action id '{action_id}'")
    if action.backend != "aws" or not action.executable:
        preview = {"op": "", "targets": [], "calls": [], "estimated_savings": 0.0}
    else:
        preview = AWSExecutor(CFG).preview(action)
    return _log_ok("preview_action", args, "any", {"ok": True, "preview": preview})


@mcp.tool()
def describe_fleet() -> dict:
    """Current EC2 fleet (mock or real, per AWS_USE_MOCK) with running count
    and total monthly cost."""
    instances = _client().describe_instances()["Reservations"][0]["Instances"]
    running = [i for i in instances if i.get("State", {}).get("Name") == "running"]
    result = {
        "ok": True,
        "instances": instances,
        "running_count": len(running),
        "monthly_cost": round(sum(i.get("MonthlyCost", 0) for i in running), 2),
    }
    return _log_ok("describe_fleet", {}, "any", result)


@mcp.tool()
def get_cost_and_usage(granularity: str = "DAILY", group_by: str = "SERVICE", days: int = 30) -> dict:
    """Cost Explorer-shaped cost data (mock or real, per AWS_USE_MOCK).
    granularity: DAILY|MONTHLY. group_by: SERVICE|REGION|INSTANCE_TYPE|TAG."""
    args = {"granularity": granularity, "group_by": group_by, "days": days}
    data = _client().get_cost_and_usage(granularity, group_by, days)
    return _log_ok("get_cost_and_usage", args, "any", {"ok": True, "data": data})


@mcp.tool()
def get_metric_statistics(namespace: str = "AWS/EC2", metric: str = "CPUUtilization",
                           hours: int = 24, instance_id: str | None = None) -> dict:
    """CloudWatch-shaped metric datapoints (mock or real, per AWS_USE_MOCK)."""
    args = {"namespace": namespace, "metric": metric, "hours": hours, "instance_id": instance_id}
    data = _client().get_metric_statistics(namespace, metric, hours, instance_id)
    return _log_ok("get_metric_statistics", args, "any", {"ok": True, "data": data})


@mcp.tool()
def list_budgets() -> dict:
    """Current AWS Budgets (mock or real, per AWS_USE_MOCK)."""
    budgets = _client().list_budgets().get("Budgets", [])
    return _log_ok("list_budgets", {}, "any", {"ok": True, "budgets": budgets})


@mcp.tool()
def commit_readiness() -> dict:
    """How close the RE queue is to being committable: how many requests are
    still undecided, how many are staged either way, and the total savings
    exposure if the batch were committed right now."""
    result = {"ok": True, **commit_status(STORE)}
    return _log_ok("commit_readiness", {}, "any", result)


@mcp.tool()
def get_decision_log(limit: int = 50) -> dict:
    """Recent raise/withdraw/stage/unstage/commit/decline/rollback activity,
    newest first — includes both human dashboard actions and MCP tool
    calls that went through this same log."""
    entries = DECISION_LOG.all()[:limit]
    return _log_ok("get_decision_log", {"limit": limit}, "any", {"ok": True, "entries": entries})


@mcp.tool()
def get_mcp_audit_log(limit: int = 50) -> dict:
    """This MCP server's own call history — every tool invocation, allowed or
    denied, by any agent. Distinct from get_decision_log, which is the
    approval workflow's log; this is every raw tool call including reads."""
    entries = audit.read_all(AUDIT_PATH, limit)
    return _log_ok("get_mcp_audit_log", {"limit": limit}, "any", {"ok": True, "entries": entries})


# --------------------------------------------------------- employee-level --

@mcp.tool()
def raise_recommendation(action_id: str, raised_by: str, role: str = "employee") -> dict:
    """Send an open ('pending') recommendation to the RE team's queue for
    review. Never touches AWS or a CSV — only marks intent to have it
    reviewed, exactly like the employee dashboard's Accept button."""
    args = {"action_id": action_id, "raised_by": raised_by, "role": role}
    action = STORE.get(action_id)
    if not action:
        return _log_error("raise_recommendation", args, role, f"Unknown action id '{action_id}'")
    try:
        action = raise_for_review(STORE, DECISION_LOG, action, raised_by)
    except ApprovalError as exc:
        return _log_error("raise_recommendation", args, role, str(exc))
    return _log_ok("raise_recommendation", args, role, {"ok": True, "action": action.to_dict()})


@mcp.tool()
def withdraw_recommendation(action_id: str, actor: str, role: str = "employee") -> dict:
    """Pull a raised request back out of the RE queue before it's decided."""
    args = {"action_id": action_id, "actor": actor, "role": role}
    action = STORE.get(action_id)
    if not action:
        return _log_error("withdraw_recommendation", args, role, f"Unknown action id '{action_id}'")
    try:
        action = withdraw(STORE, DECISION_LOG, action, actor)
    except ApprovalError as exc:
        return _log_error("withdraw_recommendation", args, role, str(exc))
    return _log_ok("withdraw_recommendation", args, role, {"ok": True, "action": action.to_dict()})


# ---------------------------------------------------------------- RE-only --

@mcp.tool()
def stage_recommendation_decision(
    action_id: str,
    decision: str,
    staged_by: str,
    re_token: str | None = None,
    reason: str | None = None,
    override: bool = False,
    role: str = "re_team",
) -> dict:
    """RE-only. Stage an approve/decline intent on a queued ('pending_re')
    request. decision must be 'approve' or 'decline' ('decline' requires
    reason). Never touches AWS or a CSV — the change lands only once every
    request in the queue is staged and commit_approved_changes() runs."""
    args = {"action_id": action_id, "decision": decision, "staged_by": staged_by,
             "re_token": re_token, "reason": reason, "override": override, "role": role}
    try:
        require_re_role(CFG, re_token)
    except NotAuthorized as exc:
        return _log_denied("stage_recommendation_decision", args, role, str(exc))
    action = STORE.get(action_id)
    if not action:
        return _log_error("stage_recommendation_decision", args, role, f"Unknown action id '{action_id}'")
    try:
        action = stage_decision(STORE, DECISION_LOG, action, decision, staged_by, reason, override)
    except ApprovalError as exc:
        return _log_error("stage_recommendation_decision", args, role, str(exc))
    return _log_ok("stage_recommendation_decision", args, role, {"ok": True, "action": action.to_dict()})


@mcp.tool()
def unstage_recommendation_decision(
    action_id: str, actor: str, re_token: str | None = None, role: str = "re_team",
) -> dict:
    """RE-only. Pull a staged approve/decline back to pending_re, before a
    commit."""
    args = {"action_id": action_id, "actor": actor, "re_token": re_token, "role": role}
    try:
        require_re_role(CFG, re_token)
    except NotAuthorized as exc:
        return _log_denied("unstage_recommendation_decision", args, role, str(exc))
    action = STORE.get(action_id)
    if not action:
        return _log_error("unstage_recommendation_decision", args, role, f"Unknown action id '{action_id}'")
    try:
        action = unstage(STORE, DECISION_LOG, action, actor)
    except ApprovalError as exc:
        return _log_error("unstage_recommendation_decision", args, role, str(exc))
    return _log_ok("unstage_recommendation_decision", args, role, {"ok": True, "action": action.to_dict()})


@mcp.tool()
def commit_approved_changes(decided_by: str, re_token: str | None = None, role: str = "re_team") -> dict:
    """RE-only. Apply every staged decision as one batch. Refuses if any
    request in the queue is still undecided ('pending_re') — the whole queue
    must be triaged (approved or declined) first. This is the only tool in
    this server that can ever change AWS or write a CSV row. A committed
    batch's reversible changes can be undone with rollback_batch_changes."""
    args = {"decided_by": decided_by, "re_token": re_token, "role": role}
    try:
        require_re_role(CFG, re_token)
    except NotAuthorized as exc:
        return _log_denied("commit_approved_changes", args, role, str(exc))
    try:
        summary = commit_batch(CFG, STORE, DECISION_LOG, decided_by)
    except ApprovalError as exc:
        return _log_error("commit_approved_changes", args, role, str(exc))
    return _log_ok("commit_approved_changes", args, role, {"ok": True, **summary})


@mcp.tool()
def rollback_batch_changes(
    batch_id: str, actor: str, re_token: str | None = None, role: str = "re_team",
) -> dict:
    """RE-only. Undo a previously committed batch's reversible changes
    (stop -> restart, resize -> resize back, tag -> restore prior tags).
    Terminated instances and non-AWS (csv-backend) actions cannot be undone
    and are reported as such rather than silently skipped."""
    args = {"batch_id": batch_id, "actor": actor, "re_token": re_token, "role": role}
    try:
        require_re_role(CFG, re_token)
    except NotAuthorized as exc:
        return _log_denied("rollback_batch_changes", args, role, str(exc))
    try:
        summary = rollback_batch(CFG, STORE, DECISION_LOG, batch_id, actor)
    except ValueError as exc:
        return _log_error("rollback_batch_changes", args, role, str(exc))
    return _log_ok("rollback_batch_changes", args, role, {"ok": True, **summary})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
