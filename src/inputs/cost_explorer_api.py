"""AWS Cost Explorer input source. Uses boto3 against the live Cost Explorer
API (only when credentials are present).

Parses the AWS-shaped ``get_cost_and_usage`` response into the four pipeline
tables, so the rest of the pipeline is identical to the CSV path.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from ..utils.logger import get_logger
from .base import InputSource, SourcePayload

log = get_logger("inputs.cost_explorer")


class CostExplorerSource(InputSource):
    kind = "cost_explorer"

    def __init__(self, region: str = "us-east-1", days: int = 30):
        self.region = region
        self.days = days

    def is_available(self) -> bool:
        return bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))

    def fetch(self) -> SourcePayload:
        return self._fetch_real()

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
