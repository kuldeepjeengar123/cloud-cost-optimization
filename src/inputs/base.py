"""Input-source interface.

Every connector (CSV, Cost Explorer, CloudWatch) returns the same shape so
later steps don't care where the data came from. ``SourcePayload.kind`` is the
only field downstream code needs to look at to apply source-specific
normalization rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

RawRecord = dict[str, Any]


@dataclass
class SourcePayload:
    kind: str
    name: str
    records: dict[str, list[RawRecord]]
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: list[str] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return sum(len(v) for v in self.records.values())


class InputSource(ABC):
    kind: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def fetch(self) -> SourcePayload: ...
