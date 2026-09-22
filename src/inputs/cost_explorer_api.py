"""AWS Cost Explorer input source.

Two modes, selected by config:

* **mock** (default) — talks to the local credential-free mock server via
  :class:`MockAWSClient`. No boto3, no AWS account, no browser login.
* **real** — uses boto3 against the live Cost Explorer API (only when
  ``aws_use_mock`` is False *and* credentials are present).

Both paths parse the same AWS-shaped ``get_cost_and_usage`` response into the
four pipeline tables, so the rest of the pipeline is identical to the CSV path.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from ..mock_aws.client import MockAWSClient
from ..utils.logger import get_logger
from .base import InputSource, SourcePayload

log = get_logger("inputs.cost_explorer")


class CostExplorerSource(InputSource):
    kind = "cost_explorer"

    def __init__(self, region: str = "us-east-1", days: int = 30,
                 use_mock: bool = True, endpoint_url: str = "http://127.0.0.1:8788"):
        self.region = region
        self.days = days
        self.use_mock = use_mock
        self.endpoint_url = endpoint_url
        self._mock = MockAWSClient(endpoint_url) if use_mock else None

    def is_available(self) -> bool:
        if self.use_mock:
            from ..mock_aws.client import ensure_mock_server
            up = ensure_mock_server(self.endpoint_url)
            if not up:
                log.warning("Mock AWS not reachable at %s and could not be "
                            "auto-started — try `python mock_aws.py`.", self.endpoint_url)
            return up
        return bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))

    def fetch(self) -> SourcePayload:
        if self.use_mock:
            return self._fetch_mock()
        return self._fetch_real()

    # ----- mock path (default) -----
    def _fetch_mock(self) -> SourcePayload:
        records: dict[str, list[dict]] = {
            "service_daily_cost": [], "region_cost": [],
            "ec2_instance_cost": [], "tag_cost": [],
        }

        # Daily cost by service -> service_daily_cost (Date, Service, Cost)
        resp = self._mock.get_cost_and_usage("DAILY", "SERVICE", self.days)
        for bucket in resp.get("ResultsByTime", []):
            day = bucket["TimePeriod"]["Start"]
            for g in bucket.get("Groups", []):
                records["service_daily_cost"].append({
                    "Date": day, "Service": g["Keys"][0],
                    "Cost": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                })

        # Cost grouped by region / instance type / project tag (monthly)
        for group_by, table, col in (
            ("REGION", "region_cost", "Region"),
            ("INSTANCE_TYPE", "ec2_instance_cost", "Instance Type"),
            ("TAG", "tag_cost", "Project Tag"),
        ):
            resp = self._mock.get_cost_and_usage("MONTHLY", group_by, self.days)
            for bucket in resp.get("ResultsByTime", []):
                for g in bucket.get("Groups", []):
                    records[table].append({
                        col: g["Keys"][0],
                        "Cost": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                    })

        return SourcePayload(
            kind=self.kind,
            name=f"mock_cost_explorer:{self.endpoint_url}",
            records=records,
            notes=[f"Mock Cost Explorer (no credentials) over last {self.days} days"],
        )

    # ----- real path (only when explicitly enabled with creds) -----
    def _fetch_real(self) -> SourcePayload:
        if not self.is_available():
            raise RuntimeError("AWS credentials not configured; cannot call real Cost Explorer.")
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for the real CostExplorerSource") from exc

        client = boto3.client(
            "ce", region_name=self.region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=self.days)
        time_period = {"Start": start.isoformat(), "End": end.isoformat()}

        records: dict[str, list[dict]] = {
            "service_daily_cost": [], "region_cost": [],
            "ec2_instance_cost": [], "tag_cost": [],
        }
        resp = client.get_cost_and_usage(
            TimePeriod=time_period, Granularity="DAILY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        for bucket in resp.get("ResultsByTime", []):
            day = bucket["TimePeriod"]["Start"]
            for group in bucket.get("Groups", []):
                records["service_daily_cost"].append({
                    "Date": day, "Service": group["Keys"][0],
                    "Cost": float(group["Metrics"]["UnblendedCost"]["Amount"]),
                })
        resp = client.get_cost_and_usage(
            TimePeriod=time_period, Granularity="MONTHLY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
        )
        for bucket in resp.get("ResultsByTime", []):
            for group in bucket.get("Groups", []):
                records["region_cost"].append({
                    "Region": group["Keys"][0],
                    "Cost": float(group["Metrics"]["UnblendedCost"]["Amount"]),
                })
        return SourcePayload(
            kind=self.kind,
            name=f"cost_explorer:{self.region}",
            records=records,
            notes=[f"Cost Explorer window: {start.isoformat()} -> {end.isoformat()}"],
        )
