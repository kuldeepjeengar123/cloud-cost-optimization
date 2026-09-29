"""
Prove the M1 -> M2 bridge works against whatever M1 has actually produced,
live - no hand-copied sample_data file, no manual decrypt step.

This script deliberately stops BEFORE calling build_pipeline_graph() / the
LLM normalise node - it only exercises the bridge (decryption + loading) and
the static schema-gap check, both pure local logic. No LLM call, no AWS
call, no network at all. See run_pipeline_real_m1_test.py for the version
that goes on to actually invoke the graph, once an OPENROUTER_API_KEY is
configured and you're ready to spend a real (if free-tier) LLM call.

Run ingestion/pipeline.py at least once first, on whatever CSV you want
(e.g. ingestion/incoming/dirty_sample.csv), so there's a real
staging/*.json.enc file for this to decrypt.
"""
from __future__ import annotations

import json
import sys

from agent_module.m1_bridge import list_m1_staging_files, load_all_m1_records
from agent_module.schemas import NormalizedCostRecord

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def print_schema_gap(record: dict) -> list:
    required_fields = [
        name for name, field in NormalizedCostRecord.model_fields.items()
        if field.is_required()
    ]
    missing = [f for f in required_fields if f not in record]
    return missing


def main() -> None:
    print("=" * 70)
    print("M1 -> M2 bridge: decrypting real M1 staging output (no LLM, no AWS)")
    print("=" * 70)

    files = list_m1_staging_files()
    print(f"Found {len(files)} staging file(s) in ingestion/staging/:")
    for f in files:
        print(f"  - {f.name}")

    records = load_all_m1_records()
    print(f"\nDecrypted {len(records)} real M1 record(s) total.\n")

    if not records:
        print("No records to check - run ingestion/pipeline.py against a CSV first.")
        return

    required_fields = [
        name for name, field in NormalizedCostRecord.model_fields.items()
        if field.is_required()
    ]
    print(f"Step 1's target schema requires: {required_fields}\n")

    for i, record in enumerate(records, start=1):
        missing = print_schema_gap(record)
        print(f"Record {i}: {json.dumps(record, default=str)}")
        print(f"  -> missing for NormalizedCostRecord: {missing or '(none)'}")

    print("\nBridge check complete - no LLM call was made. Run "
          "run_pipeline_real_m1_test.py (with OPENROUTER_API_KEY set) to "
          "actually push these through Step 1/2's LLM normalise+enrich nodes.")


if __name__ == "__main__":
    main()
