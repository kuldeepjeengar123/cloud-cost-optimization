"""RealAWSClient — the live counterpart to ``mock_aws.client.MockAWSClient``.

Same method names and same boto3-shaped return values, so ``AWSExecutor``
never has to know which one it is holding (see ``actions.executor``). Reads
(``describe_instances``, ``list_budgets``) always run once this client is
constructed — there is nothing dangerous about looking. Every *mutation*
(stop/start/resize/terminate/tag/budget) instead runs through
``_guard_write()`` first:

    1. ``cfg.aws_allow_real_writes`` must be true, or the call is logged and
       returned as a dry-run — the exact boto3 call that *would* have run,
       never executed.
    2. Even then, the target instance (when there is one) must satisfy
       ``cfg.aws_write_allowed_regions`` / ``cfg.aws_write_require_tag`` if
       either is configured — an extra allowlist so a write-enabled run still
       can't reach an instance nobody explicitly opted in.

This is the single choke point real-AWS mutations pass through, no matter
whether the caller is ``commit_batch()``, the MCP server, or anything else —
the safety gate lives here, not in any one entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..mock_aws import pricing  # generic on-demand price table, not mock-specific
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..config import PipelineConfig

log = get_logger("aws.real_client")


class RealAWSClient:
    def __init__(self, cfg: "PipelineConfig"):
        self.cfg = cfg
        self._ec2 = None
        self._budgets = None
        self._sts = None
        self._account_id: str | None = None

    # ----- lazy boto3 clients (so importing this module never requires
    # credentials to be present — only actually calling AWS does) -----
    @property
    def ec2(self):
        if self._ec2 is None:
            import boto3

            self._ec2 = boto3.client("ec2", region_name=self.cfg.aws_region)
        return self._ec2

    @property
    def budgets(self):
        if self._budgets is None:
            import boto3

            self._budgets = boto3.client("budgets", region_name=self.cfg.aws_region)
        return self._budgets

    def _account(self) -> str:
        if self._account_id is None:
            import boto3

            sts = boto3.client("sts", region_name=self.cfg.aws_region)
            self._account_id = sts.get_caller_identity()["Account"]
        return self._account_id

    # ----- reachability -----
    def ping(self) -> bool:
        try:
            import boto3

            boto3.client("sts", region_name=self.cfg.aws_region).get_caller_identity()
            return True
        except Exception as exc:  # pragma: no cover - network/credential dependent
            log.warning("Real AWS not reachable: %s", exc)
            return False

    # ----- reads -----
    def _tags_list(self, inst: dict) -> list[dict]:
        return [{"Key": t["Key"], "Value": t["Value"]} for t in inst.get("Tags", [])]

    def describe_instances(self) -> dict:
        instances: list[dict] = []
        paginator = self.ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    itype = inst.get("InstanceType", "")
                    state = inst.get("State", {}).get("Name", "unknown")
                    instances.append({
                        "InstanceId": inst["InstanceId"],
                        "InstanceType": itype,
                        "State": {"Name": state},
                        "Placement": inst.get("Placement", {}),
                        "Region": self.cfg.aws_region,
                        "Tags": inst.get("Tags", []),
                        # Best-effort heuristic cost, same table the mock uses —
                        # real per-instance billing needs Cost Explorer's
                        # resource-level data, out of scope for this dry-run
                        # plumbing pass.
                        "MonthlyCost": pricing.monthly(itype) if state == "running" else 0.0,
                        # Real per-instance CPU needs a CloudWatch call per
                        # instance; left None here to avoid an N+1 API fan-out
                        # on every fleet read. Use get_metric_statistics(
                        # instance_id=...) for a specific instance instead.
                        "CpuUtilization": None,
                    })
        return {"Reservations": [{"Instances": instances}]}

    def get_metric_statistics(self, namespace: str = "AWS/EC2",
                               metric: str = "CPUUtilization", hours: int = 24,
                               instance_id: str | None = None) -> dict:
        import boto3
        from datetime import datetime, timedelta, timezone

        cw = boto3.client("cloudwatch", region_name=self.cfg.aws_region)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        dimensions = [{"Name": "InstanceId", "Value": instance_id}] if instance_id else []
        resp = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric, Dimensions=dimensions,
            StartTime=start, EndTime=end, Period=3600,
            Statistics=["Average", "Maximum"],
        )
        return {
            "Label": metric, "Namespace": namespace,
            "Datapoints": [
                {"Timestamp": dp["Timestamp"].isoformat(), "Average": dp.get("Average"),
                 "Maximum": dp.get("Maximum"), "Unit": dp.get("Unit", "Percent")}
                for dp in resp.get("Datapoints", [])
            ],
        }

    def list_budgets(self) -> dict:
        resp = self.budgets.describe_budgets(AccountId=self._account())
        return {"Budgets": [
            {
                "name": b["BudgetName"],
                "limit": float(b.get("BudgetLimit", {}).get("Amount", 0) or 0),
                "scope": "account",
                "unit": b.get("BudgetLimit", {}).get("Unit", "USD"),
                "actual_spend": float((b.get("CalculatedSpend", {}).get("ActualSpend", {}) or {}).get("Amount", 0) or 0),
                "breached": None,
            }
            for b in resp.get("Budgets", [])
        ]}

    # ----- write guard: dry-run unless explicitly enabled, plus an
    # allowlist even when it is -----
    def _instance_meta(self, instance_id: str) -> dict | None:
        try:
            resp = self.ec2.describe_instances(InstanceIds=[instance_id])
        except Exception as exc:
            log.warning("Could not look up %s for the write guard: %s", instance_id, exc)
            return None
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                return inst
        return None

    def _guard_write(self, call_desc: str, instance_id: str | None = None) -> dict[str, Any] | None:
        """Return a blocking dry-run/guardrail response, or None to proceed."""
        if not self.cfg.aws_allow_real_writes:
            msg = f"[DRY-RUN] AWS_ALLOW_REAL_WRITES=0 — would call: {call_desc}"
            log.warning(msg)
            return {"ok": False, "message": msg}

        if instance_id and (self.cfg.aws_write_allowed_regions or self.cfg.aws_write_require_tag):
            inst = self._instance_meta(instance_id)
            if inst is None:
                return {"ok": False, "message": f"Could not verify {instance_id} against the write guardrail; blocked."}
            if self.cfg.aws_write_allowed_regions and self.cfg.aws_region not in self.cfg.aws_write_allowed_regions:
                return {"ok": False, "message": (
                    f"Blocked by write guardrail: region '{self.cfg.aws_region}' not in "
                    f"AWS_WRITE_ALLOWED_REGIONS ({', '.join(self.cfg.aws_write_allowed_regions)})."
                )}
            if self.cfg.aws_write_require_tag:
                tag_keys = {t["Key"] for t in inst.get("Tags", [])}
                if self.cfg.aws_write_require_tag not in tag_keys:
                    return {"ok": False, "message": (
                        f"Blocked by write guardrail: {instance_id} is missing the required tag "
                        f"'{self.cfg.aws_write_require_tag}'."
                    )}
        return None

    # ----- mutations -----
    def stop_instance(self, instance_id: str) -> dict:
        blocked = self._guard_write(f"ec2.stop_instances(InstanceIds=['{instance_id}'])", instance_id)
        if blocked:
            return blocked
        try:
            self.ec2.stop_instances(InstanceIds=[instance_id])
            return {"ok": True, "message": f"Stopped {instance_id}", "monthly_savings": 0.0}
        except Exception as exc:
            return {"ok": False, "message": f"stop_instances failed: {exc}"}

    def start_instance(self, instance_id: str) -> dict:
        blocked = self._guard_write(f"ec2.start_instances(InstanceIds=['{instance_id}'])", instance_id)
        if blocked:
            return blocked
        try:
            self.ec2.start_instances(InstanceIds=[instance_id])
            return {"ok": True, "message": f"Started {instance_id}"}
        except Exception as exc:
            return {"ok": False, "message": f"start_instances failed: {exc}"}

    def resize_instance(self, instance_id: str, instance_type: str | None = None) -> dict:
        new_type = instance_type or pricing.smaller_type(self._current_type(instance_id) or "")
        if not new_type:
            return {"ok": False, "message": f"No smaller instance type known for {instance_id}"}
        allowed = self.cfg.aws_write_allowed_instance_types
        if allowed and new_type not in allowed:
            msg = (f"Blocked by write guardrail: resize target '{new_type}' is not in "
                   f"AWS_WRITE_ALLOWED_INSTANCE_TYPES ({', '.join(allowed)}).")
            log.warning(msg)
            return {"ok": False, "message": msg}
        desc = (f"ec2.stop_instances(InstanceIds=['{instance_id}']); "
                f"ec2.modify_instance_attribute(InstanceId='{instance_id}', InstanceType={{'Value':'{new_type}'}}); "
                f"ec2.start_instances(InstanceIds=['{instance_id}'])")
        blocked = self._guard_write(desc, instance_id)
        if blocked:
            return blocked
        try:
            self.ec2.stop_instances(InstanceIds=[instance_id])
            waiter = self.ec2.get_waiter("instance_stopped")
            waiter.wait(InstanceIds=[instance_id])
            self.ec2.modify_instance_attribute(InstanceId=instance_id, InstanceType={"Value": new_type})
            self.ec2.start_instances(InstanceIds=[instance_id])
            return {"ok": True, "message": f"Resized {instance_id} -> {new_type}"}
        except Exception as exc:
            return {"ok": False, "message": f"resize failed: {exc}"}

    def terminate_instance(self, instance_id: str) -> dict:
        blocked = self._guard_write(f"ec2.terminate_instances(InstanceIds=['{instance_id}'])", instance_id)
        if blocked:
            return blocked
        try:
            self.ec2.terminate_instances(InstanceIds=[instance_id])
            return {"ok": True, "message": f"Terminated {instance_id}"}
        except Exception as exc:
            return {"ok": False, "message": f"terminate_instances failed: {exc}"}

    def tag_instance(self, instance_id: str, tags: dict[str, str]) -> dict:
        tag_desc = ", ".join(f"{k}={v}" for k, v in tags.items())
        desc = f"ec2.create_tags(Resources=['{instance_id}'], Tags=[{tag_desc}])"
        blocked = self._guard_write(desc, instance_id)
        if blocked:
            return blocked
        try:
            self.ec2.create_tags(
                Resources=[instance_id],
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
            )
            return {"ok": True, "message": f"Tagged {instance_id} with {tags}"}
        except Exception as exc:
            return {"ok": False, "message": f"create_tags failed: {exc}"}

    def set_budget(self, name: str, limit: float, scope: str = "service:EC2") -> dict:
        desc = f"budgets.create_budget/update_budget(BudgetName='{name}', Amount={limit})"
        blocked = self._guard_write(desc)
        if blocked:
            return blocked
        budget = {
            "BudgetName": name,
            "BudgetLimit": {"Amount": str(limit), "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
        }
        try:
            try:
                self.budgets.create_budget(AccountId=self._account(), Budget=budget)
                message = f"Created budget {name} (${limit})"
            except self.budgets.exceptions.DuplicateRecordException:
                self.budgets.update_budget(AccountId=self._account(), NewBudget=budget)
                message = f"Updated budget {name} -> ${limit}"
            return {"ok": True, "message": message}
        except Exception as exc:
            return {"ok": False, "message": f"budget write failed: {exc}"}

    # ----- helpers -----
    def _current_type(self, instance_id: str) -> str | None:
        inst = self._instance_meta(instance_id)
        return inst.get("InstanceType") if inst else None

    def reset(self) -> dict:
        return {"ok": False, "message": "reset() is a mock-only operation; real AWS has no reset."}
