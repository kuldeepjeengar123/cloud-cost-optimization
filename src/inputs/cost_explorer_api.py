"""AWS Cost Explorer input source. Uses boto3 against the live Cost Explorer
API (only when credentials are present).

Parses the AWS-shaped ``get_cost_and_usage`` response into the four pipeline
tables (service_daily_cost, region_cost, ec2_instance_cost, tag_cost) — the
same four tables the old docs/*.csv files used to provide — with matching
column names, so the rest of the pipeline (entity catalog, charts, LLM
prompts) is identical regardless of where the data came from.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from ..config import date_range_bounds
from ..utils.logger import get_logger
from .base import InputSource, SourcePayload

log = get_logger("inputs.cost_explorer")

# Fallback window when no date_range preset applies (None/unrecognized) —
# matches the old hardcoded default of a trailing 30-day window.
_DEFAULT_WINDOW_DAYS = 30


class CostExplorerSource(InputSource):
    kind = "cost_explorer"

    def __init__(self, region: str = "us-east-1", date_range: str | None = None, tag_key: str = "Project"):
        self.region = region
        # Same DATE_RANGE_DAYS preset the dashboard's date-range dropdown
        # uses (see config.date_range_bounds), anchored on the real UTC
        # "today".
        self.date_range = date_range
        # Cost allocation tag to group "tag_cost" by. Must already be
        # activated as a cost allocation tag in the account (an AWS Billing
        # console setting, not something this code can turn on) — see
        # _fetch_tag_cost's own try/except for what happens when it isn't.
        self.tag_key = tag_key

    def is_available(self) -> bool:
        return bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))

    def fetch(self) -> SourcePayload:
        return self._fetch_real()

    def _grouped_daily_cost(self, client, time_period: dict, group_by: dict) -> list[tuple[str, str, float]]:
        """One DAILY ``get_cost_and_usage`` call grouped by a single dimension
        or tag. Returns ``[(day, group_key, cost), ...]`` — every table below
        is just this reshaped under its own column names."""
        resp = client.get_cost_and_usage(
            TimePeriod=time_period, Granularity="DAILY", Metrics=["UnblendedCost"],
            GroupBy=[group_by],
        )
        rows = []
        for bucket in resp.get("ResultsByTime", []):
            day = bucket["TimePeriod"]["Start"]
            for group in bucket.get("Groups", []):
                rows.append((day, group["Keys"][0], float(group["Metrics"]["UnblendedCost"]["Amount"])))
        return rows

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
        today = datetime.now(timezone.utc).date()
        bounds = date_range_bounds(self.date_range, today)
        if bounds is None:
            bounds = (today - timedelta(days=_DEFAULT_WINDOW_DAYS - 1), today)
        start, end_inclusive = bounds
        # Cost Explorer's TimePeriod.End is exclusive (and must be strictly
        # after Start), unlike the inclusive (start, end) date_range_bounds
        # returns — so a single-day window (e.g. "today") needs End bumped
        # by one day rather than passed through as-is.
        time_period = {"Start": start.isoformat(), "End": (end_inclusive + timedelta(days=1)).isoformat()}
        notes = [f"Cost Explorer window: {start.isoformat()} -> {end_inclusive.isoformat()} (date_range={self.date_range or 'default'})"]

        records: dict[str, list[dict]] = {
            "service_daily_cost": [
                {"Date": day, "Service": key, "Cost": cost}
                for day, key, cost in self._grouped_daily_cost(
                    client, time_period, {"Type": "DIMENSION", "Key": "SERVICE"},
                )
            ],
            "region_cost": [
                {"Date": day, "Region": key, "Cost": cost}
                for day, key, cost in self._grouped_daily_cost(
                    client, time_period, {"Type": "DIMENSION", "Key": "REGION"},
                )
            ],
            "ec2_instance_cost": [
                {"Date": day, "Instance Type": key, "Cost": cost}
                for day, key, cost in self._grouped_daily_cost(
                    client, time_period, {"Type": "DIMENSION", "Key": "INSTANCE_TYPE"},
                )
            ],
        }

        # Grouping by a cost allocation tag 400s if that tag key was never
        # activated in the account's Billing console — a per-account setting
        # this code can't turn on, so this table degrades to empty rather
        # than failing the whole fetch when it isn't.
        try:
            tag_rows = self._grouped_daily_cost(
                client, time_period, {"Type": "TAG", "Key": self.tag_key},
            )
        except Exception as exc:
            log.warning(
                "tag_cost skipped: cost allocation tag '%s' unavailable (%s)",
                self.tag_key, exc,
            )
            tag_rows = []
        records["tag_cost"] = [
            # AWS returns tag group keys as "<TagKey>$<TagValue>" (or just the
            # key with an empty value for untagged resources) — "Project Tag"
            # matches the CSV column name this table used to come from.
            {"Date": day, "Project Tag": (key.split("$", 1)[1] or "Untagged") if "$" in key else key, "Cost": cost}
            for day, key, cost in tag_rows
        ]
        if not tag_rows:
            notes.append(f"tag_cost: no data for cost allocation tag '{self.tag_key}'.")

        return SourcePayload(
            kind=self.kind,
            name=f"cost_explorer:{self.region}",
            records=records,
            notes=notes,
        )
