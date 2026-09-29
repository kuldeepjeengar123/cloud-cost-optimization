"""
Common schema for raw Cost Explorer / CUR-style line items.

This is the row-level schema the pipeline validates every ingested record
against, distinct from the pivoted per-report CSVs handled by
load_csvs.py / csv_watcher.py. Fields follow the AWS CUR naming used in the
domain notes: LinkedAccountId, Service, UsageType, BlendedCost, UsageStartDate.
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator

ACCOUNT_ID_RE = r"^\d{12}$"


class CostLineItem(BaseModel):
    linked_account_id: str = Field(..., pattern=ACCOUNT_ID_RE)
    service: str = Field(..., min_length=1)
    usage_type: str = Field(..., min_length=1)
    region: Optional[str] = None
    usage_start_date: date
    blended_cost: float = Field(..., ge=0)
    usage_quantity: Optional[float] = Field(default=None, ge=0)
    tags: Optional[str] = None

    @field_validator("usage_start_date", mode="before")
    @classmethod
    def parse_iso_date(cls, v):
        # Reject anything that isn't strict ISO-8601 (YYYY-MM-DD) - pydantic's
        # default date coercion is more lenient than we want (e.g. accepts
        # "01/05/2026" via some locales); date.fromisoformat is strict.
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            try:
                return date.fromisoformat(v.strip())
            except ValueError:
                raise ValueError(f"usage_start_date must be ISO-8601 (YYYY-MM-DD), got {v!r}")
        raise ValueError(f"usage_start_date must be a string or date, got {type(v).__name__}")

    @field_validator("linked_account_id", "service", "usage_type", "region", "tags", mode="before")
    @classmethod
    def strip_strings(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v
