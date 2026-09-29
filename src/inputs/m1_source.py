"""M1 input source - the real production ingestion path.

Integration Step 11: local_csv (docs/*.csv) was a developer's own test
fixture, not real M1 output - it carries none of M1's guardrails
(validation, PII masking, account gating, encryption) at all. This source
reads M1's actual output (ingestion/staging/*.json.enc - real,
validated, PII-masked, encrypted CostLineItem records) and is the real
production path from here on; local_csv remains available for dev/testing,
just no longer the default (see config.py's PipelineConfig.sources).

M1's row-level schema (linked_account_id, service, usage_type, region,
usage_start_date, blended_cost, usage_quantity, tags - every field present
on every row at once) is richer than any single docs/*.csv table (each of
which carries exactly one dimension column alongside date+cost). Rather
than changing capabilities/anomaly_detection.py, forecasting.py, root_cause.py,
and tag_governance.py - already real, already tested, all built expecting
that one-dimension-per-table shape - this source pivots M1's row-level
output into the same shape on the way in: one row emitted per (dimension,
line item) into each of the 4 familiar tables. No data is lost - all four
pivots are derived from the same decrypted line items - it's reshaped, not
subsetted, to fit the convention every other capability already relies on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from ..utils.logger import get_logger
from .base import InputSource, RawRecord, SourcePayload

log = get_logger("inputs.m1_source")


def _load_m1_key(m1_folder: Path) -> bytes:
    key_path = m1_folder / "secret.key"
    if not key_path.exists():
        raise FileNotFoundError(
            f"No M1 encryption key at {key_path} - run ingestion/pipeline.py "
            "against at least one CSV first (it generates secret.key on first use)."
        )
    return key_path.read_bytes()


def _decrypt_file(fernet: Fernet, enc_path: Path) -> list[dict]:
    token = enc_path.read_bytes()
    payload = fernet.decrypt(token)
    return json.loads(payload.decode("utf-8"))


def _extract_project_tag(tags: Optional[str]) -> Optional[str]:
    """Pull "Project=X" out of M1's free-text "Key=Value;Key2=Value2" tags
    string, if present - mirrors docs/tag_cost.csv's single "Project Tag"
    column. Any other tag key (Owner, BackupArn, InternalAcct, ...) isn't
    relevant to this particular pivot."""
    if not tags:
        return None
    for pair in tags.split(";"):
        key, _, value = pair.partition("=")
        if key.strip().lower() == "project":
            return value.strip() or None
    return None


def _extract_instance_type(usage_type: Optional[str]) -> Optional[str]:
    """M1's usage_type is free text like "BoxUsage:t3.medium" - the part
    after the last ":" is the instance type when present. Same convention
    actions/executor.py's _INSTANCE_TYPE_RE already relies on elsewhere in
    this repo, applied here on the way in instead of the way out."""
    if not usage_type or ":" not in usage_type:
        return None
    candidate = usage_type.rsplit(":", 1)[-1].strip()
    return candidate or None


class M1Source(InputSource):
    """Reads ingestion/'s real, validated/masked/encrypted output.

    When m2_enrichment is True, each decrypted line item is additionally run
    through M2's real normalise+enrich graph (see m2_enrichment.py) before
    being pivoted - the actual "M1 -> M2" hop the docx describes, not just a
    raw field copy. Off by default: this makes real LLM + embedding calls.

    When enrichment ran, M2's business_tags (cost_centre, environment) are
    pivoted into two more tables - cost_centre_cost, environment_cost -
    alongside the four M1-only ones, so M3's capabilities (anomaly_detection,
    forecasting, root_cause, tag_governance) actually see M2's output instead
    of it being attached-but-unused. With enrichment off, "m2" is never
    present on any item and these two tables come back empty, same as today.
    """

    kind = "m1"

    def __init__(self, m1_folder: Path, m2_enrichment: bool = False):
        self.m1_folder = Path(m1_folder)
        self.staging_folder = self.m1_folder / "staging"
        self.m2_enrichment = m2_enrichment

    def is_available(self) -> bool:
        return self.staging_folder.exists() and any(self.staging_folder.glob("*.json.enc"))

    def fetch(self) -> SourcePayload:
        if not self.is_available():
            raise FileNotFoundError(f"No M1 output (*.json.enc) found in {self.staging_folder}")

        fernet = Fernet(_load_m1_key(self.m1_folder))
        line_items: list[dict] = []
        files_read: list[str] = []
        for enc_path in sorted(self.staging_folder.glob("*.json.enc")):
            line_items.extend(_decrypt_file(fernet, enc_path))
            files_read.append(enc_path.name)

        if self.m2_enrichment:
            from .m2_enrichment import enrich_line_items

            log.info("M2 enrichment enabled - running %s line item(s) through the real M2 graph", len(line_items))
            line_items = enrich_line_items(line_items)

        records: dict[str, list[RawRecord]] = {
            "service_daily_cost": [],
            "region_cost": [],
            "ec2_instance_cost": [],
            "tag_cost": [],
            "cost_centre_cost": [],
            "environment_cost": [],
        }
        for item in line_items:
            date, cost = item.get("usage_start_date"), item.get("blended_cost")
            if date is None or cost is None:
                continue

            if item.get("service"):
                records["service_daily_cost"].append({"Date": date, "Service": item["service"], "Cost": cost})
            if item.get("region"):
                records["region_cost"].append({"Date": date, "Region": item["region"], "Cost": cost})

            instance_type = _extract_instance_type(item.get("usage_type"))
            if instance_type:
                records["ec2_instance_cost"].append(
                    {"Date": date, "Instance Type": instance_type, "Cost": cost}
                )

            project_tag = _extract_project_tag(item.get("tags"))
            if project_tag:
                records["tag_cost"].append({"Date": date, "Project Tag": project_tag, "Cost": cost})

            # M2's business_tags (cost_centre, environment) - only present when
            # m2_enrichment ran and produced a result for this item (a failed
            # call leaves "m2" absent, same as m2_enrichment=False).
            business_tags = (item.get("m2") or {}).get("business_tags") or {}
            if business_tags.get("cost_centre"):
                records["cost_centre_cost"].append(
                    {"Date": date, "Cost Centre": business_tags["cost_centre"], "Cost": cost}
                )
            if business_tags.get("environment"):
                records["environment_cost"].append(
                    {"Date": date, "Environment": business_tags["environment"], "Cost": cost}
                )

        records = {table: rows for table, rows in records.items() if rows}
        for table, rows in records.items():
            log.info("Pivoted %s M1 line item(s) into %s", len(rows), table)

        notes = [f"Decrypted {len(line_items)} M1 line item(s) from {len(files_read)} staging file(s)"]
        notes.append("M2 enrichment: on" if self.m2_enrichment else "M2 enrichment: off (raw pivot)")
        return SourcePayload(
            kind=self.kind,
            name=f"m1:{self.staging_folder}",
            records=records,
            notes=notes,
        )
