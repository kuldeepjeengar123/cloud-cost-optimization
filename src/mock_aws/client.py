"""MockAWSClient — thin HTTP client the pipeline uses instead of boto3.

Same *shape* of responses as boto3 (``ResultsByTime``/``Groups`` for Cost
Explorer, ``Datapoints`` for CloudWatch, ``Reservations`` for EC2), so swapping
to real AWS later is a base-URL change plus re-pointing at boto3 — the parsing
code in the input sources doesn't change.

No credentials. No signing. Just HTTP against the local mock server.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import requests

from ..utils.logger import get_logger

log = get_logger("mock_aws.client")


def ensure_mock_server(endpoint_url: str, wait_s: float = 4.0) -> bool:
    """Make sure a mock server is answering at ``endpoint_url``.

    If one is already up, return True. If the endpoint is local and nothing is
    listening, auto-start an embedded server in a background thread and wait for
    it to come up. Remote endpoints are never auto-started.
    """
    client = MockAWSClient(endpoint_url)
    if client.ping():
        return True
    parsed = urlparse(endpoint_url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False
    from .server import serve_in_background
    serve_in_background(parsed.port or 8788)
    deadline = wait_s
    while deadline > 0:
        if client.ping():
            return True
        time.sleep(0.15)
        deadline -= 0.15
    return client.ping()


class MockAWSClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8788", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ----- reachability -----
    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/health", timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # ----- reads (boto3-shaped) -----
    def get_cost_and_usage(self, granularity: str = "DAILY",
                           group_by: str = "SERVICE", days: int = 30) -> dict:
        return self._get("/aws/ce/cost-and-usage",
                         {"granularity": granularity, "group_by": group_by, "days": days})

    def get_metric_statistics(self, namespace: str = "AWS/EC2",
                              metric: str = "CPUUtilization", hours: int = 24,
                              instance_id: str | None = None) -> dict:
        params: dict[str, Any] = {"namespace": namespace, "metric": metric, "hours": hours}
        if instance_id:
            params["instance_id"] = instance_id
        return self._get("/aws/cw/metric-statistics", params)

    def describe_instances(self) -> dict:
        return self._get("/aws/ec2/instances", {})

    def list_budgets(self) -> dict:
        return self._get("/aws/budgets", {})

    # ----- mutations -----
    def stop_instance(self, instance_id: str) -> dict:
        return self._post(f"/aws/ec2/instances/{instance_id}/stop")

    def start_instance(self, instance_id: str) -> dict:
        return self._post(f"/aws/ec2/instances/{instance_id}/start")

    def resize_instance(self, instance_id: str, instance_type: str | None = None) -> dict:
        body = {"instance_type": instance_type} if instance_type else {}
        return self._post(f"/aws/ec2/instances/{instance_id}/resize", body)

    def terminate_instance(self, instance_id: str) -> dict:
        return self._post(f"/aws/ec2/instances/{instance_id}/terminate")

    def tag_instance(self, instance_id: str, tags: dict[str, str]) -> dict:
        return self._post(f"/aws/ec2/instances/{instance_id}/tags", {"tags": tags})

    def set_budget(self, name: str, limit: float, scope: str = "service:EC2") -> dict:
        return self._post("/aws/budgets", {"name": name, "limit": limit, "scope": scope})

    def reset(self) -> dict:
        return self._post("/aws/reset")

    # ----- transport -----
    def _get(self, path: str, params: dict) -> dict:
        r = requests.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None) -> dict:
        r = requests.post(f"{self.base_url}{path}", json=body or {}, timeout=self.timeout)
        # mutations return 400 with an explanatory body on bad input — surface it
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
