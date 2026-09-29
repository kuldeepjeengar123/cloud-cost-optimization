"""
Row-level validation + PII-masking pipeline for raw Cost Explorer / CUR-style
CSVs (the common schema in models.py), with file-level rate limiting.

Run: python pipeline.py dirty_sample.csv
Outputs:
    processed/clean_<name>.csv     - validated rows, PII masked (plaintext, for inspection)
    processed/rejected_<name>.csv  - rows that failed validation or the account gate, with the reason
    staging/<name>.json.enc        - the same accepted rows as Fernet-encrypted JSON (the real M2 handoff artifact)
    processing_log.jsonl           - one append-only audit entry per file processed
"""
import csv
import logging
import sys
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from accounts import check_account
from encryption import encrypt_json
from models import CostLineItem
from pii import mask_pii
from processing_log import log_file
from rate_limiter import RateLimiter

FOLDER = Path(__file__).parent
PROCESSED = FOLDER / "processed"
STAGING = FOLDER / "staging"

FIELD_MAP = {
    "LinkedAccountId": "linked_account_id",
    "Service": "service",
    "UsageType": "usage_type",
    "Region": "region",
    "UsageStartDate": "usage_start_date",
    "BlendedCost": "blended_cost",
    "UsageQuantity": "usage_quantity",
    "Tags": "tags",
}
MODEL_FIELDS = list(FIELD_MAP.values())

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("pipeline")


def _to_model_kwargs(row: dict) -> dict:
    kwargs = {}
    for csv_col, field in FIELD_MAP.items():
        value = row.get(csv_col, "")
        kwargs[field] = value if value not in ("", None) else None
    return kwargs


def process_file(path: Path, rate_limiter: Optional[RateLimiter] = None) -> dict:
    if rate_limiter is not None:
        logger.info(f"Waiting for a rate-limit slot to process {path.name}...")
        rate_limiter.acquire()

    PROCESSED.mkdir(exist_ok=True)
    STAGING.mkdir(exist_ok=True)
    clean_path = PROCESSED / f"clean_{path.name}"
    rejected_path = PROCESSED / f"rejected_{path.name}"
    staging_path = STAGING / f"{path.stem}.json.enc"

    row_count = 0
    accepted = 0
    validation_rejected = 0
    denylist_rejected = 0
    pii_hits = 0
    clean_records = []

    with open(path, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        raw_fieldnames = reader.fieldnames or []

        with open(clean_path, "w", newline="", encoding="utf-8") as f_clean, \
                open(rejected_path, "w", newline="", encoding="utf-8") as f_rejected:
            clean_writer = csv.DictWriter(f_clean, fieldnames=MODEL_FIELDS)
            clean_writer.writeheader()
            rejected_writer = csv.DictWriter(f_rejected, fieldnames=raw_fieldnames + ["error"])
            rejected_writer.writeheader()

            for row_number, row in enumerate(reader, start=2):  # row 1 is the header
                row_count += 1
                try:
                    item = CostLineItem(**_to_model_kwargs(row))
                except ValidationError as e:
                    reason = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
                    logger.warning(f"Row {row_number} rejected: {reason}")
                    rejected_writer.writerow({**row, "error": reason})
                    validation_rejected += 1
                    continue

                account_error = check_account(item.linked_account_id)
                if account_error:
                    logger.warning(f"Row {row_number} rejected: {account_error}")
                    rejected_writer.writerow({**row, "error": account_error})
                    denylist_rejected += 1
                    continue

                record = item.model_dump()
                if record["tags"]:
                    masked, found = mask_pii(record["tags"])
                    if found:
                        logger.info(f"Row {row_number}: masked PII in tags ({', '.join(found)})")
                        pii_hits += 1
                    record["tags"] = masked

                clean_writer.writerow(record)
                clean_records.append(record)
                accepted += 1

    encrypt_json(clean_records, staging_path)

    log_entry = log_file(
        file=path.name,
        row_count=row_count,
        accepted=accepted,
        validation_failures=validation_rejected,
        denylist_rejections=denylist_rejected,
        pii_hits=pii_hits,
        output_path=str(staging_path.relative_to(FOLDER)),
    )

    summary = {
        "file": path.name,
        "accepted": accepted,
        "validation_rejected": validation_rejected,
        "denylist_rejected": denylist_rejected,
        "pii_masked_rows": pii_hits,
        "clean_output": str(clean_path.relative_to(FOLDER)),
        "rejected_output": str(rejected_path.relative_to(FOLDER)),
        "staging_output": str(staging_path.relative_to(FOLDER)),
        "log_entry": log_entry,
    }
    logger.info(
        f"{path.name}: {accepted} accepted, {validation_rejected} failed validation, "
        f"{denylist_rejected} blocked by account gate, {pii_hits} row(s) had PII masked "
        f"-> {staging_path.name} (encrypted)"
    )
    return summary


def main():
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <file.csv> [<file2.csv> ...]")
        sys.exit(1)

    files = [Path(p) for p in sys.argv[1:]]
    rate_limiter = RateLimiter(max_per_minute=5)
    for f in files:
        process_file(f, rate_limiter)


if __name__ == "__main__":
    main()
