"""End-to-end smoke test for the batch approval gate.

Exercises the real approval/executor/rollback code paths — raise -> stage ->
commit -> verify the mock fleet actually changed -> roll back -> verify it's
undone — without invoking the LLM pipeline (so it's fast, free, and doesn't
depend on network/API-key availability). Runs against an isolated mock AWS
instance and an isolated outputs folder so it never touches the real demo
state in outputs/pending_actions.json or outputs/mock_state.json.

    python scripts/smoke_test.py

Exits 0 and prints "SMOKE TEST PASSED" on success; raises on the first failed
assertion otherwise.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.actions.approval import ApprovalError, commit_batch, commit_status, raise_for_review, stage_decision
from src.actions.decision_log import DecisionLogStore
from src.actions.models import STATUS_APPLIED, STATUS_DECLINED, STATUS_PENDING_RE, STATUS_STAGED_APPROVE, Action
from src.actions.rollback import rollback_batch
from src.actions.store import ActionStore
from src.config import load_config
from src.mock_aws.client import MockAWSClient, ensure_mock_server

MOCK_PORT = 8799
SMOKE_ROOT = Path(__file__).resolve().parent.parent / "outputs" / "_smoke_test"


def _fresh_cfg():
    """Build an isolated config AND actually isolate the mock server's state.

    ``mock_aws.server`` keeps its ``STATE``/``STATE_PATH`` as *module-level*
    globals resolved once from the process's default config — a different
    ``aws_endpoint_url``/port does NOT give a different state file, since the
    handler always reads/writes the same module globals regardless of which
    config asked for it. Pointing this test at a different port without also
    swapping those globals would silently reset and mutate the real demo's
    outputs/mock_state.json. So: swap the module's state to an isolated file
    before the embedded server ever starts handling requests.
    """
    if SMOKE_ROOT.exists():
        shutil.rmtree(SMOKE_ROOT)
    cfg = load_config(
        output_folder=SMOKE_ROOT,
        applied_folder=SMOKE_ROOT / "applied",
        aws_endpoint_url=f"http://127.0.0.1:{MOCK_PORT}",
        action_backend="aws",
        aws_use_mock=True,
    )

    import src.mock_aws.server as mock_server_module
    from src.mock_aws.state import MockState

    state_path = cfg.output_folder / "mock_state.json"
    mock_server_module.STATE_PATH = state_path
    mock_server_module.DOCS_FOLDER = cfg.docs_folder
    mock_server_module.STATE = MockState.load(state_path, cfg.docs_folder)

    assert ensure_mock_server(cfg.aws_endpoint_url), "could not start the mock AWS server"
    MockAWSClient(cfg.aws_endpoint_url).reset()
    return cfg


def main() -> int:
    cfg = _fresh_cfg()
    store = ActionStore(cfg.output_folder / "pending_actions.json")
    log = DecisionLogStore(cfg.output_folder / "decision_log.json")
    client = MockAWSClient(cfg.aws_endpoint_url)
    run_id = "smoke_run_001"

    # --- Build three actions directly (bypassing the LLM/planner text-match
    # step — this test targets the approval/executor/rollback layer, not the
    # planner's NLP matching, which has no bearing on this feature). ---
    a_stop = Action.for_row(
        run_id, "Stop idle legacy-idle (t3.xlarge)", "ec2_instance_cost",
        "instance_type", "t3.xlarge", {"action_status": "applied"}, impact="high", backend="aws",
    )
    a_decline = Action.for_row(
        run_id, "Rightsize analytics-1 (r5.xlarge)", "ec2_instance_cost",
        "instance_type", "r5.xlarge", {"action_status": "applied"}, impact="medium", backend="aws",
    )
    a_fail = Action.for_row(
        run_id, "Rightsize worker-1 (r5.large) one tier down", "ec2_instance_cost",
        "instance_type", "r5.large", {"action_status": "applied"}, impact="low", backend="aws",
    )
    store.upsert_many([a_stop, a_decline, a_fail])

    # --- Employee raises all three ---
    for a in (a_stop, a_decline, a_fail):
        a = raise_for_review(store, log, store.get(a.id), "smoke-employee")
        assert a.status == STATUS_PENDING_RE

    # --- Commit must refuse while anything is undecided ---
    try:
        commit_batch(cfg, store, log, "smoke-re-team")
        raise AssertionError("commit_batch should have refused with undecided requests")
    except ApprovalError:
        pass

    # --- RE stages: approve the stop, decline the resize, approve the
    # doomed-to-fail resize (r5.large has no smaller tier in the mock) ---
    stage_decision(store, log, store.get(a_stop.id), "approve", "smoke-re-team")
    stage_decision(store, log, store.get(a_decline.id), "decline", "smoke-re-team", reason="Prod node, not now.")
    stage_decision(store, log, store.get(a_fail.id), "approve", "smoke-re-team")

    status = commit_status(store)
    assert status["undecided"] == 0, status
    assert status["ready"], status

    # --- Commit the batch ---
    summary = commit_batch(cfg, store, log, "smoke-re-team")
    assert summary["applied"] == 1, summary   # only the stop succeeds
    assert summary["declined"] == 1, summary
    assert summary["failed"] == 1, summary
    batch_id = summary["batch_id"]

    stopped = store.get(a_stop.id)
    assert stopped.status == STATUS_APPLIED, stopped.status
    declined = store.get(a_decline.id)
    assert declined.status == STATUS_DECLINED, declined.status
    failed = store.get(a_fail.id)
    assert failed.status == STATUS_STAGED_APPROVE, failed.status  # left staged for retry

    # --- Verify the mock fleet actually changed ---
    inv = client.describe_instances()["Reservations"][0]["Instances"]
    legacy = next(i for i in inv if i["InstanceType"] == "t3.xlarge" or i["InstanceId"] == "i-1a7b8c9d")
    assert legacy["State"]["Name"] == "stopped", legacy

    # --- Roll back the batch and verify it's undone ---
    rb = rollback_batch(cfg, store, log, batch_id, "smoke-re-team")
    assert any(r["ok"] for r in rb["results"]), rb
    inv = client.describe_instances()["Reservations"][0]["Instances"]
    legacy = next(i for i in inv if i["InstanceId"] == "i-1a7b8c9d")
    assert legacy["State"]["Name"] == "running", legacy

    print("SMOKE TEST PASSED")
    print(f"  batch_id={batch_id}  applied=1 declined=1 failed(left-staged)=1  rollback_ok=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
