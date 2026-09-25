"""AWS CloudWatch input source. Uses boto3 against the live CloudWatch API."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from ..utils.logger import get_logger
from .base import InputSource, SourcePayload

log = get_logger("inputs.cloudwatch")


class CloudWatchSource(InputSource):
    kind = "cloudwatch"

    def __init__(self, region: str = "us-west-2", hours: int = 24):
        self.region = region
        self.hours = hours

    def is_available(self) -> bool:
        return bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))

    def fetch(self) -> SourcePayload:
        return self._fetch_real()

    def _fetch_real(self) -> SourcePayload:
        if not self.is_available():
            raise RuntimeError("AWS credentials not configured; cannot call real CloudWatch.")
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for the real CloudWatchSource") from exc

        cw = boto3.client(
            "cloudwatch", region_name=self.region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=self.hours)
        targets = {
            "AWS/EC2": ["CPUUtilization", "NetworkIn", "NetworkOut"],
            "AWS/Lambda": ["Invocations", "Errors", "Duration"],
            "AWS/RDS": ["CPUUtilization", "DatabaseConnections"],
        }
        records: dict[str, list[dict]] = {"cloudwatch_metrics": []}
        for namespace, metric_names in targets.items():
            for metric_name in metric_names:
                try:
                    resp = cw.get_metric_statistics(
                        Namespace=namespace, MetricName=metric_name,
                        StartTime=start, EndTime=end, Period=3600,
                        Statistics=["Average", "Sum", "Maximum"],
                    )
                except Exception as exc:
                    log.warning("CloudWatch %s/%s failed: %s", namespace, metric_name, exc)
                    continue
                for dp in resp.get("Datapoints", []):
                    records["cloudwatch_metrics"].append({
                        "Namespace": namespace, "Metric": metric_name,
                        "Timestamp": dp["Timestamp"].isoformat(),
                        "Average": dp.get("Average"), "Sum": dp.get("Sum"),
                        "Maximum": dp.get("Maximum"),
                    })
        return SourcePayload(
            kind=self.kind,
            name=f"cloudwatch:{self.region}",
            records=records,
            notes=[f"CloudWatch window: {start.isoformat()} -> {end.isoformat()}"],
        )
