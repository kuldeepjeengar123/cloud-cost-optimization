"""Executors — apply an Action against a backend.

``get_executor(cfg)`` returns the executor selected by ``cfg.action_backend``:

    "csv" -> CSVExecutor: annotate the matching row in docs/<table>.csv
    "aws" -> AWSExecutor: stop/start instances & change config via boto3 (stub)

Both implement ``apply(action) -> ApplyResult``. The Action descriptor and the
"Apply" button are identical across backends — only this layer changes when you
flip from the CSV simulation to real AWS calls.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import PipelineConfig
from ..utils.logger import get_logger
from .models import (
    STATUS_ACKNOWLEDGED,
    STATUS_APPLIED,
    Action,
)

log = get_logger("actions.executor")


@dataclass
class ApplyResult:
    ok: bool
    message: str
    action: Action


def _norm_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CSVExecutor:
    """Phase 1: apply an approved change to the matching CSV row.

    Reads from the pristine source ``docs/<table>.csv`` but writes the modified
    copy into ``applied_folder/<run_id>/<table>.csv`` — the source data is never
    mutated. Multiple approved actions touching the same table in one run
    accumulate against the working copy in that run's folder.
    """

    backend = "csv"

    def __init__(self, docs_folder: Path, applied_folder: Path):
        self.docs_folder = Path(docs_folder)
        self.applied_folder = Path(applied_folder)

    def _working_path(self, run_id: str, table: str) -> Path:
        return self.applied_folder / (run_id or "adhoc") / f"{table}.csv"

    def apply(self, action: Action) -> ApplyResult:
        if not action.executable or not action.table:
            action.status = STATUS_ACKNOWLEDGED
            action.applied_at = _now()
            action.result_note = "Manual recommendation — acknowledged (no row to change)."
            return ApplyResult(True, action.result_note, action)

        out_path = self._working_path(action.run_id, action.table)
        # Read the working copy if a prior action in this run already wrote it,
        # otherwise start from the pristine source CSV.
        source_path = self.docs_folder / f"{action.table}.csv"
        read_path = out_path if out_path.exists() else source_path
        if not read_path.exists():
            return ApplyResult(False, f"CSV not found: {source_path.name}", action)

        with open(read_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

        # Locate the source column whose normalized name matches the action.
        col = next(
            (fn for fn in fieldnames if _norm_key(fn) == action.match_column),
            None,
        )
        if col is None:
            return ApplyResult(
                False, f"Column '{action.match_column}' not in {source_path.name}", action
            )

        # Fields written on apply (original keys preserved as new columns).
        writes = {**action.set_fields, "applied_at": _now()}
        for key in writes:
            if key not in fieldnames:
                fieldnames.append(key)

        target = (action.match_value or "").strip().lower()
        matched = 0
        for row in rows:
            if str(row.get(col, "")).strip().lower() == target:
                row.update({k: str(v) for k, v in writes.items()})
                matched += 1

        if not matched:
            return ApplyResult(
                False,
                f"No row in {source_path.name} where {col} = '{action.match_value}'",
                action,
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        action.status = STATUS_APPLIED
        action.applied_at = writes["applied_at"]
        rel = out_path.relative_to(self.applied_folder.parent)
        action.result_note = (
            f"Wrote {matched} updated row(s) to {rel} "
            f"({col} = {action.match_value}); source CSV left untouched."
        )
        log.info(action.result_note)
        return ApplyResult(True, action.result_note, action)


class AWSExecutor:
    """Apply an approved recommendation against the AWS API.

    By default this drives the **mock** AWS server (credential-free), so an
    approved "stop the idle instance" actually flips that instance to stopped
    and the next cost read drops — the real-time loop the demo is about. It
    resolves the recommendation to concrete instances by matching the action's
    target (instance type / region / project tag) against the live inventory,
    then picks an operation (stop / resize / terminate / tag / budget) from the
    recommendation wording.

    With ``aws_use_mock=False`` it drives :class:`aws.real_client.RealAWSClient`
    — real reads always run; every real *write* additionally requires
    ``cfg.aws_allow_real_writes`` or it comes back as a logged dry-run (see
    that module's docstring for the full guardrail).
    """

    backend = "aws"

    # recommendation keyword -> operation. Order matters (first hit wins).
    _OP_KEYWORDS = [
        (("terminat", "delete", "remove", "decommission", "unused"), "terminate"),
        (("resize", "rightsize", "right-size", "downsize", "smaller", "right size"), "resize"),
        (("stop", "shut", "idle", "pause", "turn off", "power off"), "stop"),
        (("tag", "allocation"), "tag"),
        (("budget", "anomaly", "alert", "threshold"), "budget"),
    ]

    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        if cfg.aws_use_mock:
            from ..mock_aws.client import MockAWSClient
            self._client = MockAWSClient(cfg.aws_endpoint_url)
        else:
            from ..aws.real_client import RealAWSClient
            self._client = RealAWSClient(cfg)

    def _op_for(self, title: str) -> str:
        low = title.lower()
        for keywords, op in self._OP_KEYWORDS:
            if any(k in low for k in keywords):
                return op
        return "stop"  # most universal cost reducer

    def _match_instances(self, action: Action, inventory: list[dict]) -> list[dict]:
        col, val = action.match_column, (action.match_value or "").lower()

        def tag(inst, key):
            return next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == key), "")

        running = [i for i in inventory if i.get("State", {}).get("Name") == "running"]
        if col == "instance_type":
            return [i for i in running if i.get("InstanceType", "").lower() == val]
        if col == "region":
            return [i for i in running if i.get("Region", "").lower() == val]
        if col in ("project_tag", "project"):
            return [i for i in running if tag(i, "Project").lower() == val]
        if col == "service" and ("ec2" in val or "compute" in val):
            return running  # service-level EC2 recommendation -> whole fleet
        # last resort: substring match against any visible field
        return [i for i in running
                if val and (val in i.get("InstanceType", "").lower()
                            or val in i.get("Region", "").lower()
                            or val in tag(i, "Project").lower())]

    def _ensure_reachable(self) -> bool:
        """Mock: auto-start/verify the local server. Real: always True — a
        real client's own calls surface their own connectivity errors."""
        if not self.cfg.aws_use_mock:
            return True
        from ..mock_aws.client import ensure_mock_server
        return ensure_mock_server(self.cfg.aws_endpoint_url)

    def matched_instances(self, action: Action) -> list[dict]:
        """Full instance dicts (with Tags) an action would touch — used by the
        risk guardrail, which needs tag data that ``preview()``'s id-only
        ``targets`` list discards."""
        if not self._ensure_reachable():
            return []
        try:
            inventory = self._client.describe_instances()["Reservations"][0]["Instances"]
        except Exception as exc:
            log.warning("Could not read the fleet for %s (%s)", action.id, exc)
            return []
        if not action.op:
            action.op = self._op_for(action.title)
        return self._match_instances(action, inventory)

    def preview(self, action: Action) -> dict:
        """Read-only plan: which op, which instances, which calls, roughly how
        much it would save. Never mutates anything — safe to call before an
        action is even raised for approval, so the RE dashboard can show
        exactly what approving it will do.
        """
        op = self._op_for(action.title)
        empty = {"op": op, "targets": [], "calls": [], "estimated_savings": 0.0}
        if op == "budget":
            return {**empty, "calls": [
                'POST /aws/budgets  {"name":"auto-from-recommendation","limit":100.0,"scope":"service:EC2"}'
            ]}
        if not self._ensure_reachable():
            return empty
        try:
            inventory = self._client.describe_instances()["Reservations"][0]["Instances"]
        except Exception as exc:
            log.warning("Could not read the fleet for preview of %s (%s)", action.id, exc)
            return empty
        targets = self._match_instances(action, inventory)
        calls, saved = [], 0.0
        for inst in targets:
            iid = inst["InstanceId"]
            cost = float(inst.get("MonthlyCost", 0) or 0)
            if op == "terminate":
                calls.append(f"POST /aws/ec2/instances/{iid}/terminate")
                saved += cost
            elif op == "resize":
                calls.append(f"POST /aws/ec2/instances/{iid}/stop")
                calls.append(f"POST /aws/ec2/instances/{iid}/resize")
                calls.append(f"POST /aws/ec2/instances/{iid}/start")
                from ..mock_aws import pricing
                smaller = pricing.smaller_type(inst.get("InstanceType", ""))
                saved += max(0.0, cost - pricing.monthly(smaller)) if smaller else 0.0
            elif op == "tag":
                calls.append(f'POST /aws/ec2/instances/{iid}/tags  {{"CostOptimized":"true"}}')
            else:  # stop
                calls.append(f"POST /aws/ec2/instances/{iid}/stop")
                saved += cost
        return {"op": op, "targets": [i["InstanceId"] for i in targets],
                "calls": calls, "estimated_savings": round(saved, 2)}

    def apply(self, action: Action) -> ApplyResult:
        if not self._ensure_reachable():
            msg = (f"Mock AWS not reachable at {self.cfg.aws_endpoint_url} and "
                   f"could not be auto-started. Try `python mock_aws.py`.")
            action.result_note = msg
            return ApplyResult(False, msg, action)

        op = self._op_for(action.title)

        # Budget/anomaly recommendations aren't instance-specific.
        if op == "budget":
            res = self._client.set_budget("auto-from-recommendation", 100.0, "service:EC2")
            if not res.get("ok"):
                # Left staged (see executor.apply's caller, commit_batch): a
                # failed/dry-run write must be retryable, not recorded as if
                # it had happened.
                action.result_note = f"Budget write failed: {res.get('message', '')} Left staged for retry."
                return ApplyResult(False, action.result_note, action)
            return self._finish(action, True, f"Set budget alert (EC2 > $100/mo). {res.get('message', '')}")

        try:
            inventory = self._client.describe_instances()["Reservations"][0]["Instances"]
        except Exception as exc:
            msg = f"Could not read the fleet ({exc}); left staged for retry."
            action.result_note = msg
            return ApplyResult(False, msg, action)
        targets = self._match_instances(action, inventory)
        if not targets:
            action.status = STATUS_ACKNOWLEDGED
            action.applied_at = _now()
            action.result_note = (f"No matching live instances for "
                                   f"{action.match_column}='{action.match_value}'. Acknowledged.")
            return ApplyResult(True, action.result_note, action)

        saved, notes, all_ok = 0.0, [], True
        for inst in targets:
            iid = inst["InstanceId"]
            if op == "terminate":
                res = self._client.terminate_instance(iid)
            elif op == "resize":
                res = self._client.resize_instance(iid)  # one tier down
            elif op == "tag":
                res = self._client.tag_instance(iid, {"CostOptimized": "true"})
            else:
                res = self._client.stop_instance(iid)
            if res.get("ok"):
                saved += float(res.get("monthly_savings", 0) or 0)
                notes.append(res.get("message", ""))
            else:
                all_ok = False
                notes.append(f"FAILED {iid}: {res.get('message', 'failed')}")

        new_total = self._fleet_cost()
        summary = (f"{op.title()} {len(targets)} instance(s); "
                   f"~${saved:.2f}/mo saved. Fleet now ${new_total:.2f}/mo. "
                   + "; ".join(n for n in notes if n))
        if not all_ok:
            # Partial (or total) failure across the targeted instances: don't
            # report success — the caller (commit_batch) leaves the action
            # staged so it can be retried instead of recording it as applied.
            action.result_note = summary + " Left staged for retry."
            return ApplyResult(False, action.result_note, action)
        return self._finish(action, True, summary)

    def _fleet_cost(self) -> float:
        try:
            inv = self._client.describe_instances()["Reservations"][0]["Instances"]
        except Exception as exc:
            log.warning("Could not read fleet cost after apply (%s)", exc)
            return 0.0
        return round(sum(i.get("MonthlyCost", 0) for i in inv
                         if i.get("State", {}).get("Name") == "running"), 2)

    def _finish(self, action: Action, ok: bool, note: str) -> ApplyResult:
        action.status = STATUS_APPLIED if ok else STATUS_ACKNOWLEDGED
        action.applied_at = _now()
        action.result_note = note
        log.info("AWS apply: %s", note)
        return ApplyResult(ok, note, action)


def get_executor(cfg: PipelineConfig, backend: str | None = None):
    """Pick the executor. ``backend`` overrides ``cfg.action_backend`` so a
    stored action can be applied with the backend it was planned for, even if
    the server's default differs."""
    chosen = backend or cfg.action_backend
    if chosen == "aws":
        return AWSExecutor(cfg)
    return CSVExecutor(cfg.docs_folder, cfg.applied_folder)
