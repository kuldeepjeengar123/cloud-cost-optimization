"""M2 enrichment - wires the real M2 module (agent_module/) into the src/
pipeline as the actual "M1 -> M2" hop the docx describes: M2's normalise +
enrich nodes run on each M1 line item before M3 ever sees it. Until this
module existed, m1_source.py's pivot was a raw field copy with no M2
involvement at all - a reasonable stand-in when M1 was first unified
(integration Step 11), explicitly noted then as not yet wired to M2.

Off by default (cfg.capabilities.m2_enrichment). Unlike every other
capabilities.* toggle in this repo, which gate purely local computation,
this one gates REAL external calls: one LLM completion (normalise, cache-
assisted - repeats are free) and one embeddings call (enrich's RAG lookup)
per unique M1 line item, plus a one-time cost the first time the module is
used at all in a process - build_pipeline_graph() constructs a FAISS
vectorstore by embedding every sample_data/csv_schemas/*.csv and
pricing_docs/*.pdf file. That first-build cost is paid once per process
(see _get_m2_graph's module-level cache), never per record.

M2 lives in the top-level agents/ folder (renamed from agent_module/ during
the folder reorg, integration Step 13 - its inner Python package is still
named agent_module, so every `from agent_module.xxx import ...` elsewhere in
that folder's own scripts kept working unchanged; only the container
folder's name, and therefore this file's sys.path entry, changed). It's a
separate top-level Python package, not a src/ submodule - sys.path is
extended to import it directly, in-process, rather than duplicating its
logic the way m1_bridge.py/output_guardrails.py deliberately duplicate M1's
small, stable regex/Fernet patterns. M2's graph is neither small nor stable
enough to duplicate - it's a real, evolving LangGraph agent with LLM
clients, a vector store, and a semantic cache - so this file imports it
directly instead.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from ..utils.logger import get_logger

log = get_logger("inputs.m2_enrichment")

_AGENT_MODULE_ROOT = Path(__file__).resolve().parent.parent.parent / "agents"

_graph = None  # built once per process - see _get_m2_graph()


def _get_m2_graph():
    """Build (once) and cache the M2 normalise+enrich graph. Building it is
    itself a real cost (embeds every schema CSV / pricing PDF into the FAISS
    vectorstore) - paid once per process, not once per record."""
    global _graph
    if _graph is not None:
        return _graph

    if str(_AGENT_MODULE_ROOT) not in sys.path:
        sys.path.insert(0, str(_AGENT_MODULE_ROOT))

    from agent_module.pipeline import build_pipeline_graph

    log.info("Building the M2 normalise+enrich graph (one-time cost: embeds schema docs into FAISS)...")
    _graph = build_pipeline_graph()
    return _graph


def enrich_line_items(line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run each M1 line item through M2's real normalise+enrich graph.

    Returns a NEW list of dicts - every original M1 field is kept exactly as
    it was (usage_start_date, blended_cost, service, region, usage_type,
    tags, linked_account_id all stay put, so m1_source.py's pivot logic is
    completely unaffected), plus M2's output attached under an "m2" key:
        {"normalized_record": {...} | None, "business_tags": {...} | None,
         "pricing_context": [...] | None, "cache_hit": bool | None}

    M2's NormalizedCostRecord schema requires fields M1 doesn't carry at all
    (resource_id, currency, period_end, usage_unit - see integration Step 2's
    finding) - the LLM has to invent those on every call. Rather than lean on
    those invented values anywhere, only the two genuinely useful, mostly-
    deterministic pieces matter here: business_tags (cost_centre/environment
    - a plain dict lookup against business_rules.json once the LLM has
    renamed linked_account_id -> account_id) and pricing_context (real RAG
    snippets). normalized_record is attached for inspection/audit only - not
    relied on by anything downstream yet.
    """
    graph = _get_m2_graph()
    enriched: list[dict[str, Any]] = []

    for item in line_items:
        initial_state = {
            "raw_record": item,
            "normalized_record": None,
            "cache_hit": None,
            "normalise_latency_seconds": None,
            "business_tags": None,
            "pricing_context": None,
        }
        result = graph.invoke(initial_state)
        enriched.append({
            **item,
            "m2": {
                "normalized_record": result.get("normalized_record"),
                "business_tags": result.get("business_tags"),
                "pricing_context": result.get("pricing_context"),
                "cache_hit": result.get("cache_hit"),
            },
        })
        if not result.get("cache_hit"):
            log.info(
                "M2 enriched a new record (%.2fs): cost_centre=%s",
                result.get("normalise_latency_seconds") or 0.0,
                (result.get("business_tags") or {}).get("cost_centre"),
            )

    return enriched
