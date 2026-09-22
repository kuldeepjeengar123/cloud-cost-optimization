"""MockState — the mutable, persistent world the fake AWS serves.

The canonical state is an **EC2 fleet** plus a seeded **baseline** of non-EC2
service costs (read once from ``docs/service_daily_cost.csv`` and
``docs/region_cost.csv``). Every cost table the pipeline consumes is *derived*
from this state, so a mutation (stop / resize / terminate / tag) is reflected
immediately the next time anything reads costs — that is the "real-time" loop.

State is seeded on first boot, then persisted to ``outputs/mock_state.json``
after every mutation so changes survive restarts. ``reset()`` re-seeds.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..utils.logger import get_logger
from . import pricing

log = get_logger("mock_aws.state")

EC2_SERVICE = "Amazon Elastic Compute Cloud - Compute"
DEFAULT_WINDOW_DAYS = 30

# A deterministic demo fleet. Costs are material on purpose so stop/resize/
# terminate produce visibly different cost charts. Regions mirror region_cost.csv.
_SEED_FLEET: list[dict[str, Any]] = [
    {"id": "i-0a1f2e3d", "name": "web-prod-1",   "instance_type": "m5.xlarge", "region": "eu-north-1", "tags": {"Project": "Atlas",  "Env": "prod"}, "cpu": 55},
    {"id": "i-0b2e3f4c", "name": "web-prod-2",   "instance_type": "m5.xlarge", "region": "eu-north-1", "tags": {"Project": "Atlas",  "Env": "prod"}, "cpu": 48},
    {"id": "i-0c3d4e5b", "name": "api-prod-1",   "instance_type": "c5.xlarge", "region": "us-east-1",  "tags": {"Project": "Atlas",  "Env": "prod"}, "cpu": 62},
    {"id": "i-0d4c5b6a", "name": "worker-1",     "instance_type": "r5.large",  "region": "us-east-1",  "tags": {"Project": "Helios", "Env": "prod"}, "cpu": 40},
    {"id": "i-0e5b6a7f", "name": "batch-1",      "instance_type": "t3.large",  "region": "eu-west-2",  "tags": {"Project": "Helios", "Env": "dev"},  "cpu": 22},
    {"id": "i-0f6a7b8e", "name": "dev-sandbox",  "instance_type": "t3.medium", "region": "eu-west-2",  "tags": {"Project": "Nimbus", "Env": "dev"},  "cpu": 8},
    {"id": "i-1a7b8c9d", "name": "legacy-idle",  "instance_type": "t3.xlarge", "region": "us-east-1",  "tags": {"Project": "Atlas",  "Env": "dev"},  "cpu": 2},
    {"id": "i-2b8c9d0e", "name": "analytics-1",  "instance_type": "r5.xlarge", "region": "eu-north-1", "tags": {"Project": "Helios", "Env": "prod"}, "cpu": 35},
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MockState:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    # ----- lifecycle -----
    @classmethod
    def load(cls, state_path: Path, docs_folder: Path) -> "MockState":
        state_path = Path(state_path)
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                log.info("Loaded mock state from %s (%d instances)",
                         state_path, len(data.get("instances", [])))
                return cls(data)
            except Exception as exc:  # corrupt file -> reseed rather than crash
                log.warning("Mock state at %s unreadable (%s); re-seeding.", state_path, exc)
        state = cls(cls._seed(docs_folder))
        state.save(state_path)
        return state

    @classmethod
    def _seed(cls, docs_folder: Path) -> dict[str, Any]:
        """Build fresh state: demo fleet + baseline non-EC2 service/region costs."""
        docs_folder = Path(docs_folder)
        service_baseline = cls._read_service_baseline(docs_folder / "service_daily_cost.csv")
        region_baseline = cls._read_region_baseline(docs_folder / "region_cost.csv")
        instances = []
        for spec in _SEED_FLEET:
            inst = dict(spec)
            inst["state"] = "running"
            inst["launched"] = _now().isoformat()
            instances.append(inst)
        log.info("Seeded mock AWS: %d EC2 instances, %d baseline services.",
                 len(instances), len(service_baseline))
        return {
            "version": 1,
            "seeded_at": _now().isoformat(),
            "window_days": DEFAULT_WINDOW_DAYS,
            "instances": instances,
            "budgets": [
                {"name": "monthly-ec2-budget", "limit": 500.0, "scope": "service:EC2", "unit": "USD"},
            ],
            "service_baseline": service_baseline,  # non-EC2 services, total over window
            "region_baseline": region_baseline,    # non-EC2 region costs (static seed)
        }

    @staticmethod
    def _read_service_baseline(path: Path) -> dict[str, float]:
        """Sum docs/service_daily_cost.csv by service, dropping EC2 (we model it live)."""
        totals: dict[str, float] = {}
        if not path.exists():
            return totals
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                service = (row.get("Service") or "").strip()
                if not service or service.startswith("Amazon Elastic Compute Cloud"):
                    continue  # EC2 compute is derived from the live fleet
                try:
                    totals[service] = totals.get(service, 0.0) + float(row.get("Cost") or 0)
                except ValueError:
                    continue
        return {k: round(v, 6) for k, v in totals.items()}

    @staticmethod
    def _read_region_baseline(path: Path) -> dict[str, float]:
        baseline: dict[str, float] = {}
        if not path.exists():
            return baseline
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                region = (row.get("Region") or "").strip()
                if not region:
                    continue
                try:
                    baseline[region] = round(float(row.get("Cost") or 0), 6)
                except ValueError:
                    continue
        return baseline

    def save(self, state_path: Path) -> None:
        state_path = Path(state_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")

    def reset(self, docs_folder: Path) -> None:
        self.data = self._seed(docs_folder)

    # ----- convenience accessors -----
    @property
    def instances(self) -> list[dict]:
        return self.data.setdefault("instances", [])

    @property
    def window_days(self) -> int:
        return int(self.data.get("window_days", DEFAULT_WINDOW_DAYS))

    def _running(self) -> list[dict]:
        return [i for i in self.instances if i.get("state") == "running"]

    def find_instance(self, instance_id: str) -> dict | None:
        return next((i for i in self.instances if i.get("id") == instance_id), None)

    # ----- cost math (everything derives from the live fleet) -----
    @staticmethod
    def _monthly(inst: dict) -> float:
        return pricing.monthly(inst.get("instance_type", ""))

    @staticmethod
    def _daily(inst: dict) -> float:
        return round(pricing.hourly(inst.get("instance_type", "")) * 24, 6)

    def ec2_monthly_total(self) -> float:
        return round(sum(self._monthly(i) for i in self._running()), 6)

    def ec2_daily_total(self) -> float:
        return round(sum(self._daily(i) for i in self._running()), 6)

    # ----- AWS-shaped responses (mimic boto3) -----
    def cost_and_usage(self, granularity: str = "DAILY",
                       group_by: str = "SERVICE", days: int | None = None) -> dict:
        """Mimic ce.get_cost_and_usage(). ``group_by`` ∈ {SERVICE, REGION,
        INSTANCE_TYPE, TAG} (TAG groups by the Project tag)."""
        days = days or self.window_days
        group_by = (group_by or "SERVICE").upper()
        end = _now().date()
        start = end - timedelta(days=days)

        def _bucket(period_start, period_end, groups: dict[str, float]) -> dict:
            return {
                "TimePeriod": {"Start": period_start, "End": period_end},
                "Total": {},
                "Groups": [
                    {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": f"{v:.10f}", "Unit": "USD"}}}
                    for k, v in groups.items() if v or k  # keep zero-cost keys for visibility
                ],
                "Estimated": False,
            }

        results: list[dict] = []

        if group_by == "SERVICE" and granularity.upper() == "DAILY":
            baseline = self.data.get("service_baseline", {})
            ec2_per_day = self.ec2_daily_total()
            for d in range(days):
                day = (start + timedelta(days=d)).isoformat()
                nxt = (start + timedelta(days=d + 1)).isoformat()
                groups = {svc: round(total / days, 10) for svc, total in baseline.items()}
                if ec2_per_day:
                    groups[EC2_SERVICE] = ec2_per_day
                results.append(_bucket(day, nxt, groups))
        elif group_by == "SERVICE":
            baseline = dict(self.data.get("service_baseline", {}))
            baseline[EC2_SERVICE] = round(self.ec2_daily_total() * days, 6)
            results.append(_bucket(start.isoformat(), end.isoformat(), baseline))
        elif group_by == "REGION":
            groups = dict(self.data.get("region_baseline", {}))
            for inst in self._running():
                r = inst.get("region", "unknown")
                groups[r] = round(groups.get(r, 0.0) + self._monthly(inst) * days / 30.0, 6)
            results.append(_bucket(start.isoformat(), end.isoformat(), groups))
        elif group_by == "INSTANCE_TYPE":
            groups: dict[str, float] = {}
            for inst in self._running():
                t = inst.get("instance_type", "unknown")
                groups[t] = round(groups.get(t, 0.0) + self._monthly(inst) * days / 30.0, 6)
            results.append(_bucket(start.isoformat(), end.isoformat(), groups))
        elif group_by.startswith("TAG"):
            groups = {}
            for inst in self._running():
                tag = inst.get("tags", {}).get("Project", "untagged")
                groups[tag] = round(groups.get(tag, 0.0) + self._monthly(inst) * days / 30.0, 6)
            results.append(_bucket(start.isoformat(), end.isoformat(), groups))
        else:
            results.append(_bucket(start.isoformat(), end.isoformat(), {}))

        return {
            "GroupDefinitions": [{"Type": "DIMENSION", "Key": group_by}],
            "ResultsByTime": results,
        }

    def metric_statistics(self, namespace: str = "AWS/EC2",
                          metric_name: str = "CPUUtilization",
                          hours: int = 24, instance_id: str | None = None) -> dict:
        """Mimic cloudwatch.get_metric_statistics() with deterministic datapoints."""
        end = _now().replace(minute=0, second=0, microsecond=0)
        running = self._running()
        if instance_id:
            running = [i for i in running if i.get("id") == instance_id]

        datapoints: list[dict] = []
        if namespace == "AWS/EC2" and metric_name == "CPUUtilization" and running:
            base = sum(i.get("cpu", 30) for i in running) / len(running)
            for h in range(hours):
                ts = (end - timedelta(hours=hours - h)).isoformat()
                # deterministic diurnal wave around the fleet's average CPU
                avg = max(0.0, min(100.0, base + 12 * math.sin(h / 3.0)))
                datapoints.append({
                    "Timestamp": ts,
                    "Average": round(avg, 2),
                    "Maximum": round(min(100.0, avg + 9), 2),
                    "Unit": "Percent",
                })
        return {
            "Label": metric_name,
            "Namespace": namespace,
            "Datapoints": datapoints,
        }

    def describe_instances(self) -> dict:
        """Mimic ec2.describe_instances() (flattened: one Reservation)."""
        return {
            "Reservations": [{
                "Instances": [
                    {
                        "InstanceId": i["id"],
                        "InstanceType": i["instance_type"],
                        "State": {"Name": i.get("state", "running")},
                        "Placement": {"AvailabilityZone": i.get("region", "") + "a"},
                        "Region": i.get("region", ""),
                        "Tags": [{"Key": k, "Value": v} for k, v in i.get("tags", {}).items()]
                                + [{"Key": "Name", "Value": i.get("name", "")}],
                        "MonthlyCost": self._monthly(i) if i.get("state") == "running" else 0.0,
                        "CpuUtilization": i.get("cpu"),
                    }
                    for i in self.instances
                ]
            }],
        }

    # ----- mutations (the "do changes in real-time" surface) -----
    def stop_instance(self, instance_id: str) -> dict:
        inst = self.find_instance(instance_id)
        if not inst:
            return {"ok": False, "message": f"No such instance: {instance_id}"}
        before = self._monthly(inst)
        inst["state"] = "stopped"
        return {"ok": True, "message": f"Stopped {inst['name']} ({instance_id})",
                "monthly_savings": round(before, 6), "instance": inst}

    def start_instance(self, instance_id: str) -> dict:
        inst = self.find_instance(instance_id)
        if not inst:
            return {"ok": False, "message": f"No such instance: {instance_id}"}
        inst["state"] = "running"
        return {"ok": True, "message": f"Started {inst['name']} ({instance_id})",
                "monthly_cost": self._monthly(inst), "instance": inst}

    def resize_instance(self, instance_id: str, instance_type: str | None = None) -> dict:
        inst = self.find_instance(instance_id)
        if not inst:
            return {"ok": False, "message": f"No such instance: {instance_id}"}
        old_type = inst["instance_type"]
        new_type = instance_type or pricing.smaller_type(old_type)
        if not new_type:
            return {"ok": False, "message": f"{inst['name']} ({old_type}) is already smallest in its family"}
        if new_type not in pricing.HOURLY_PRICE:
            return {"ok": False, "message": f"Unknown instance type: {new_type}"}
        before, after = pricing.monthly(old_type), pricing.monthly(new_type)
        inst["instance_type"] = new_type
        return {"ok": True,
                "message": f"Resized {inst['name']} {old_type} -> {new_type}",
                "monthly_savings": round(before - after, 6), "instance": inst}

    def terminate_instance(self, instance_id: str) -> dict:
        inst = self.find_instance(instance_id)
        if not inst:
            return {"ok": False, "message": f"No such instance: {instance_id}"}
        before = self._monthly(inst) if inst.get("state") == "running" else 0.0
        self.instances.remove(inst)
        return {"ok": True, "message": f"Terminated {inst['name']} ({instance_id})",
                "monthly_savings": round(before, 6), "instance": inst}

    def tag_instance(self, instance_id: str, tags: dict[str, str]) -> dict:
        inst = self.find_instance(instance_id)
        if not inst:
            return {"ok": False, "message": f"No such instance: {instance_id}"}
        inst.setdefault("tags", {}).update({str(k): str(v) for k, v in tags.items()})
        return {"ok": True, "message": f"Tagged {inst['name']} with {tags}", "instance": inst}

    # ----- budgets -----
    def list_budgets(self) -> dict:
        ec2 = self.ec2_monthly_total()
        budgets = []
        for b in self.data.get("budgets", []):
            actual = ec2 if b.get("scope") == "service:EC2" else None
            budgets.append({**b, "actual_spend": actual,
                            "breached": bool(actual is not None and actual > b.get("limit", 0))})
        return {"Budgets": budgets}

    def set_budget(self, name: str, limit: float, scope: str = "service:EC2") -> dict:
        budgets = self.data.setdefault("budgets", [])
        for b in budgets:
            if b.get("name") == name:
                b["limit"] = float(limit)
                b["scope"] = scope
                return {"ok": True, "message": f"Updated budget {name} -> ${limit}", "budget": b}
        b = {"name": name, "limit": float(limit), "scope": scope, "unit": "USD"}
        budgets.append(b)
        return {"ok": True, "message": f"Created budget {name} (${limit})", "budget": b}
