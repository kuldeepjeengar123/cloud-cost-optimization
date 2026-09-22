"""On-demand hourly prices for the mock EC2 fleet.

Approximate us-east-1 Linux on-demand rates (USD/hour). Only used to make the
simulated cost numbers feel realistic — they are NOT billed against anything.
``resize`` walks the family ladder so "rightsize one tier down" is well-defined.
"""

from __future__ import annotations

# instance_type -> USD/hour
HOURLY_PRICE: dict[str, float] = {
    "t2.nano": 0.0058,
    "t2.micro": 0.0116,
    "t2.medium": 0.0464,
    "t3.nano": 0.0052,
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "m5.2xlarge": 0.384,
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "c5.2xlarge": 0.34,
    "r5.large": 0.126,
    "r5.xlarge": 0.252,
    "r5.2xlarge": 0.504,
}

# Each family ordered small -> large so "one tier down" is unambiguous.
# t2's ladder stops at medium on purpose — see aws_write_allowed_instance_types
# in config.py, which caps real resizes to nano/micro/medium only.
_FAMILY_LADDER: dict[str, list[str]] = {
    "t2": ["t2.nano", "t2.micro", "t2.medium"],
    "t3": ["t3.nano", "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge", "t3.2xlarge"],
    "m5": ["m5.large", "m5.xlarge", "m5.2xlarge"],
    "c5": ["c5.large", "c5.xlarge", "c5.2xlarge"],
    "r5": ["r5.large", "r5.xlarge", "r5.2xlarge"],
}

HOURS_PER_MONTH = 730  # AWS billing convention (24 * 365 / 12)


def hourly(instance_type: str) -> float:
    return HOURLY_PRICE.get(instance_type, 0.0)


def monthly(instance_type: str) -> float:
    return round(hourly(instance_type) * HOURS_PER_MONTH, 6)


def smaller_type(instance_type: str) -> str | None:
    """Return the next-smaller type in the same family, or None if already smallest."""
    family = instance_type.split(".")[0]
    ladder = _FAMILY_LADDER.get(family)
    if not ladder or instance_type not in ladder:
        return None
    idx = ladder.index(instance_type)
    return ladder[idx - 1] if idx > 0 else None
