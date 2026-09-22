"""Step 1 — Simplify & Normalize Response (LLM-assisted).

The LLM is asked to (a) describe each table in plain text, (b) propose a
common-schema mapping for the records, and (c) flag inconsistencies. The raw
records still flow through unchanged; the LLM's output is attached as the
``schema_map`` so Step 2 (Context Load) can apply it.

The deterministic capability passes (validation/dedup/normalization) are
applied here too — they're cheap and the LLM works better on cleaner data.
"""

from __future__ import annotations

import json

from ..capabilities import deduplicate_records, normalize_records, validate_records
from ..config import PipelineConfig
from ..inputs.base import SourcePayload
from ..llm.client import LLMClient
from ..utils.logger import get_logger

log = get_logger("pipeline.step1")


SYSTEM_PROMPT = (
    "You are a data normalization assistant for AWS operations data. "
    "Given multiple raw record tables, you must propose a unified schema and "
    "flag inconsistencies. Reply with STRICT JSON only — no prose, no markdown."
)


def _schema_prompt(payload: SourcePayload) -> str:
    sample = {table: rows[:3] for table, rows in payload.records.items()}
    return (
        "Source kind: "
        + payload.kind
        + "\nTables and sample rows (max 3 per table):\n"
        + json.dumps(sample, default=str, indent=2)
        + "\n\nReturn JSON with this shape: {\n"
        '  "tables": {<table_name>: {"description": str, "common_schema": {<canonical_field>: <source_field>}, "issues": [str]}},\n'
        '  "global_notes": [str]\n}'
    )


def run_step1_normalize(
    cfg: PipelineConfig, payloads: list[SourcePayload], llm: LLMClient
) -> dict:
    log.info("Step 1: simplify & normalize across %s source(s)", len(payloads))

    merged: dict[str, list[dict]] = {}
    source_meta: list[dict] = []
    for payload in payloads:
        source_meta.append(
            {
                "kind": payload.kind,
                "name": payload.name,
                "fetched_at": payload.fetched_at,
                "total_records": payload.total_records,
                "notes": payload.notes,
            }
        )
        for table, rows in payload.records.items():
            merged.setdefault(table, []).extend(rows)

    if cfg.capabilities.validate:
        merged = validate_records(merged)
    if cfg.capabilities.deduplicate:
        merged = deduplicate_records(merged)
    if cfg.capabilities.normalize:
        merged = normalize_records(merged)

    schema_map: dict = {"tables": {}, "global_notes": []}
    try:
        # Build a temporary payload-shape for the prompt
        combined = SourcePayload(
            kind=",".join(p.kind for p in payloads) or "unknown",
            name="combined",
            records=merged,
        )
        raw = llm.complete(
            system=SYSTEM_PROMPT,
            user=_schema_prompt(combined),
            model=cfg.llm.model_normalize,
            max_tokens=1500,
        )
        parsed = LLMClient.extract_json(raw)
        if isinstance(parsed, dict):
            schema_map = parsed
        else:
            log.warning("Step 1 LLM did not return valid JSON; using empty schema map.")
    except Exception as exc:
        log.warning("Step 1 LLM call failed (%s); proceeding without schema map.", exc)

    return {
        "records": merged,
        "schema_map": schema_map,
        "sources": source_meta,
    }
