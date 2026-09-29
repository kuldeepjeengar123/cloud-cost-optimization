"""Append-only processing log - one JSON line per file processed.

Each entry records what Week 4's guardrails need visible for audit: when
the file arrived, how many rows it had, how many failed validation, how
many had PII masked, how many were blocked by the account denylist, and
where the (encrypted) output landed.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

FOLDER = Path(__file__).parent
LOG_PATH = FOLDER / "processing_log.jsonl"


def log_file(
    *,
    file: str,
    row_count: int,
    accepted: int,
    validation_failures: int,
    denylist_rejections: int,
    pii_hits: int,
    output_path: str,
) -> dict:
    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "file": file,
        "row_count": row_count,
        "accepted": accepted,
        "validation_failures": validation_failures,
        "denylist_rejections": denylist_rejections,
        "pii_hits": pii_hits,
        "output_path": output_path,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
