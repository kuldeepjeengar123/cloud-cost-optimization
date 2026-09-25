"""Local CSV input source.

Reads every ``*.csv`` file inside the configured docs folder and emits a
``SourcePayload`` keyed by CSV stem. Numeric-looking string fields are coerced
to floats so the analysis step can aggregate without re-parsing.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import DATE_RANGE_DAYS
from ..utils.logger import get_logger
from .base import InputSource, RawRecord, SourcePayload

log = get_logger("inputs.local_csv")


class LocalCSVSource(InputSource):
    kind = "local_csv"

    def __init__(self, docs_folder: Path, date_range: Optional[str] = None):
        self.docs_folder = Path(docs_folder)
        # Rolling window (see DATE_RANGE_DAYS), anchored on the latest Date
        # found in each file — the CSVs are a fixed historical demo window,
        # so anchoring on the real calendar date would filter everything out.
        self.date_range = date_range

    def is_available(self) -> bool:
        return self.docs_folder.exists() and any(self.docs_folder.glob("*.csv"))

    def fetch(self) -> SourcePayload:
        if not self.is_available():
            raise FileNotFoundError(f"No CSV files in {self.docs_folder}")

        records: dict[str, list[RawRecord]] = {}
        for csv_path in sorted(self.docs_folder.glob("*.csv")):
            rows = self._read_csv(csv_path)
            rows = _filter_by_date_range(rows, self.date_range)
            if rows:
                records[csv_path.stem] = rows
                log.info("Loaded %s rows from %s", len(rows), csv_path.name)

        notes = [f"Loaded {len(records)} CSV files from {self.docs_folder}"]
        if self.date_range:
            notes.append(f"Scoped to date_range={self.date_range}")

        return SourcePayload(
            kind=self.kind,
            name=f"local_csv:{self.docs_folder.name}",
            records=records,
            notes=notes,
        )

    @staticmethod
    def _read_csv(path: Path) -> list[RawRecord]:
        rows: list[RawRecord] = []
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                row: RawRecord = {}
                for key, value in raw.items():
                    row[key] = _coerce(value)
                rows.append(row)
        return rows


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _filter_by_date_range(rows: list[RawRecord], date_range: Optional[str]) -> list[RawRecord]:
    if not date_range:
        return rows
    dated = [(row, _parse_date(row.get("Date"))) for row in rows]
    anchor_candidates = [d for _, d in dated if d is not None]
    if not anchor_candidates:
        return rows
    anchor = max(anchor_candidates)

    if date_range == "yesterday":
        start = end = anchor - timedelta(days=1)
    elif date_range in DATE_RANGE_DAYS:
        end = anchor
        start = anchor - timedelta(days=DATE_RANGE_DAYS[date_range] - 1)
    else:
        return rows

    return [row for row, d in dated if d is not None and start <= d <= end]


def _coerce(value):
    if value is None:
        return None
    s = value.strip() if isinstance(value, str) else value
    if s == "":
        return None
    try:
        if isinstance(s, str) and s.replace(".", "", 1).replace("-", "", 1).isdigit():
            return float(s)
    except AttributeError:
        pass
    return s
