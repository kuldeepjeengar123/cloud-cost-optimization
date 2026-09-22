"""Mock AWS — a credential-free, in-process simulation of the AWS APIs.

This package lets the whole agent loop run against a *fake* AWS that behaves
like the real one (Cost Explorer, CloudWatch, EC2) but never touches real
credentials, never opens a browser, and never spends a cent. It exists purely
for demos and local development.

Layout
------
pricing.py  -> instance-type -> hourly on-demand price table
state.py    -> MockState: the mutable world (EC2 fleet, tags, budgets),
               seeded from docs/*.csv, persisted to outputs/mock_state.json
server.py   -> a standalone REST server exposing AWS-shaped JSON endpoints
client.py   -> MockAWSClient: a thin HTTP client that the pipeline uses in
               place of boto3 (swap base_url for a real endpoint to go live)
"""

from .client import MockAWSClient
from .state import MockState

__all__ = ["MockAWSClient", "MockState"]
