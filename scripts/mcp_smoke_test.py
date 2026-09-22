"""End-to-end smoke test for the MCP server's tool functions.

Calls the MCP tool functions directly (they're plain Python functions —
@mcp.tool() registers and returns them unchanged) rather than going through
the stdio transport, so this is fast and needs no MCP client. Exercises the
full raise -> stage -> commit -> rollback path and the RE-only gate, against
an isolated mock AWS instance (same isolation approach as scripts/
smoke_test.py — see its docstring for why that's necessary).

    python scripts/mcp_smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MOCK_PORT = 8798
SMOKE_ROOT = Path(__file__).resolve().parent.parent / "outputs" / "_mcp_smoke_test"


def _isolate(cfg):
    import src.mock_aws.server as mock_server_module
    from src.mock_aws.state import MockState

    state_path = cfg.output_folder / "mock_state.json"
    mock_server_module.STATE_PATH = state_path
    mock_server_module.DOCS_FOLDER = cfg.docs_folder
    mock_server_module.STATE = MockState.load(state_path, cfg.docs_folder)


def main() -> int:
    if SMOKE_ROOT.exists():
        shutil.rmtree(SMOKE_ROOT)

    from src.config import load_config

    cfg = load_config(
        output_folder=SMOKE_ROOT,
        applied_folder=SMOKE_ROOT / "applied",
        aws_endpoint_url=f"http://127.0.0.1:{MOCK_PORT}",
        action_backend="aws",
        aws_use_mock=True,
        re_team_token="smoke-secret",  # exercise the RE-only gate for real
    )
    _isolate(cfg)

    from src.mock_aws.client import MockAWSClient, ensure_mock_server
    assert ensure_mock_server(cfg.aws_endpoint_url)
    MockAWSClient(cfg.aws_endpoint_url).reset()

    # Point the mcp_server module at this isolated config/store instead of
    # its own process-default one.
    from src.actions.decision_log import DecisionLogStore
    from src.actions.store import ActionStore
    from src.mcp_server import server as mcpsrv

    mcpsrv.CFG = cfg
    mcpsrv.STORE = ActionStore(cfg.output_folder / "pending_actions.json")
    mcpsrv.DECISION_LOG = DecisionLogStore(cfg.output_folder / "decision_log.json")
    mcpsrv.AUDIT_PATH = cfg.output_folder / "mcp_audit.jsonl"

    from src.actions.models import Action

    a = Action.for_row(
        "mcp_smoke_run", "Stop idle legacy-idle (t3.xlarge)", "ec2_instance_cost",
        "instance_type", "t3.xlarge", {"action_status": "applied"}, impact="high", backend="aws",
    )
    mcpsrv.STORE.upsert_many([a])

    # --- reads ---
    fleet = mcpsrv.describe_fleet()
    assert fleet["ok"] and fleet["running_count"] == 8, fleet

    lst = mcpsrv.list_recommendations()
    assert lst["ok"] and len(lst["actions"]) == 1, lst

    preview = mcpsrv.preview_action(a.id)
    assert preview["ok"] and preview["preview"]["op"] == "stop", preview

    # --- raise (employee-level, no token needed) ---
    raised = mcpsrv.raise_recommendation(a.id, "mcp-agent")
    assert raised["ok"] and raised["action"]["status"] == "pending_re", raised

    # --- RE-only tools must refuse without the token ---
    denied = mcpsrv.stage_recommendation_decision(a.id, "approve", "mcp-re-agent")
    assert denied["ok"] is False and "RE team credentials" in denied["error"], denied

    # --- and succeed with it ---
    staged = mcpsrv.stage_recommendation_decision(a.id, "approve", "mcp-re-agent", re_token="smoke-secret")
    assert staged["ok"] and staged["action"]["status"] == "staged_approve", staged

    readiness = mcpsrv.commit_readiness()
    assert readiness["ok"] and readiness["ready"], readiness

    # --- commit denied without token, succeeds with it ---
    denied = mcpsrv.commit_approved_changes("mcp-re-agent")
    assert denied["ok"] is False, denied
    summary = mcpsrv.commit_approved_changes("mcp-re-agent", re_token="smoke-secret")
    assert summary["ok"] and summary["applied"] == 1, summary
    batch_id = summary["batch_id"]

    fleet = mcpsrv.describe_fleet()
    stopped = next(i for i in fleet["instances"] if i["InstanceId"] == "i-1a7b8c9d")
    assert stopped["State"]["Name"] == "stopped", stopped

    # --- rollback ---
    rb = mcpsrv.rollback_batch_changes(batch_id, "mcp-re-agent", re_token="smoke-secret")
    assert rb["ok"], rb
    fleet = mcpsrv.describe_fleet()
    restarted = next(i for i in fleet["instances"] if i["InstanceId"] == "i-1a7b8c9d")
    assert restarted["State"]["Name"] == "running", restarted

    # --- audit trail recorded both the denied and allowed calls ---
    audit_log = mcpsrv.get_mcp_audit_log()
    assert audit_log["ok"]
    tools_seen = {e["tool"] for e in audit_log["entries"]}
    assert "commit_approved_changes" in tools_seen
    denied_entries = [e for e in audit_log["entries"] if not e["allowed"]]
    assert denied_entries, "expected at least one denied call in the audit log"

    print("MCP SMOKE TEST PASSED")
    print(f"  tools exercised: describe_fleet, list_recommendations, preview_action,")
    print(f"  raise_recommendation, stage_recommendation_decision (denied+allowed),")
    print(f"  commit_readiness, commit_approved_changes (denied+allowed),")
    print(f"  rollback_batch_changes, get_mcp_audit_log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
