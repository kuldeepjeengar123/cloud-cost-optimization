from __future__ import annotations

from langchain_core.tools import tool

# In-memory cost data for the Week 2 tools exercise. Mirrors the kind of
# aggregated, current-period spend a real FinOps system would hold in memory
# or a fast cache, keyed by AWS service name.
_KNOWN_SERVICE_COSTS = {
    "Amazon EC2": 1523.47,
    "Amazon S3": 42.10,
    "Amazon RDS": 310.00,
}


@tool
def get_service_cost(service: str) -> str:
    """Look up the current billed spend (in USD) for an AWS service from
    in-memory cost data. Pass the exact service name, e.g. 'Amazon EC2'."""
    cost = _KNOWN_SERVICE_COSTS.get(service)
    if cost is None:
        known = ", ".join(_KNOWN_SERVICE_COSTS)
        return f"No cost data found for service '{service}'. Known services: {known}."
    return f"{service} spend is ${cost:.2f} USD for the current billing period."


@tool
def check_spend_threshold(service: str, threshold_usd: float) -> str:
    """Check whether an AWS service's current spend exceeds a given USD
    threshold. Pass the exact service name and the threshold amount."""
    cost = _KNOWN_SERVICE_COSTS.get(service)
    if cost is None:
        known = ", ".join(_KNOWN_SERVICE_COSTS)
        return f"No cost data found for service '{service}'. Known services: {known}."
    if cost > threshold_usd:
        return (
            f"Yes, {service} spend (${cost:.2f}) exceeds the ${threshold_usd:.2f} "
            f"threshold by ${cost - threshold_usd:.2f}."
        )
    return f"No, {service} spend (${cost:.2f}) does not exceed the ${threshold_usd:.2f} threshold."
