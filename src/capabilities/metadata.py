"""Metadata tracking — source/time/lineage for every pipeline run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MetadataTracker:
    run_started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    sources: list[dict] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)

    def record_source(self, kind: str, name: str, total_records: int, notes: list[str]) -> None:
        self.sources.append(
            {"kind": kind, "name": name, "total_records": total_records, "notes": notes}
        )

    def record_stage(self, stage: str, details: dict | None = None) -> None:
        self.stages.append(
            {
                "stage": stage,
                "at": datetime.now(timezone.utc).isoformat(),
                "details": details or {},
            }
        )

    def snapshot(self) -> dict:
        return {
            "run_started_at": self.run_started_at,
            "sources": self.sources,
            "stages": self.stages,
        }


def attach_metadata(payload: dict, tracker: MetadataTracker) -> dict:
    return {**payload, "_metadata": tracker.snapshot()}
