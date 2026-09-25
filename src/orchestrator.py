"""Pipeline orchestrator — runs the full flow from inputs to final response.

Mirrors the diagram exactly:
    Inputs -> Step 1 -> Step 2 -> (Step 3.1 || 3.2 -> 3.3) -> Step 4 -> Step 5

An optional ``on_event`` callback receives ``(event_kind, payload)`` at every
step boundary so a frontend can stream progress to the user.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from .actions import ActionStore, plan_actions
from .capabilities.forecasting import forecast_costs
from .capabilities.metadata import MetadataTracker
from .capabilities.root_cause import explain_anomalies
from .capabilities.tag_governance import check_tag_governance
from .config import PipelineConfig
from .inputs import build_sources
from .inputs.base import SourcePayload
from .integrations.teams import notify_teams
from .llm.client import LLMClient
from .pipeline import (
    run_step1_normalize,
    run_step2_context_load,
    run_step3_1_charts,
    run_step3_2_analysis,
    run_step3_3_summary,
    run_step4_combine,
    run_step5_finalize,
)
from .utils.logger import get_logger

log = get_logger("orchestrator")

EventCallback = Callable[[str, dict], None]

# Friendly labels surfaced to the frontend. The verbs that animate in the UI
# are picked client-side so we don't have to coordinate timing here.
STEP_LABELS = {
    "inputs":         ("Loading input sources",          "Sources loaded"),
    "step1":          ("Simplifying & normalizing",      "Normalized"),
    "step2":          ("Loading context & correlations", "Context loaded"),
    "forecast":       ("Forecasting cost trend",         "Forecast ready"),
    "tag_governance": ("Checking tag governance",        "Tags checked"),
    "root_cause":     ("Correlating anomaly root cause",  "Root cause ready"),
    "step3_1":        ("Generating chart specs",         "Charts drafted"),
    "step3_2":        ("Crunching analysis & metrics",   "Analysis ready"),
    "step3_3":        ("Writing executive summary",      "Summary written"),
    "step4":          ("Combining all responses",        "Combined"),
    "step5":          ("Finalizing report",              "Report ready"),
}


def _emit(on_event: Optional[EventCallback], kind: str, payload: dict) -> None:
    if on_event is not None:
        try:
            on_event(kind, payload)
        except Exception as exc:  # frontend hiccups must never break the pipeline
            log.warning("on_event raised: %s", exc)


# Module-level (not closures) so they're independently unit-testable — each
# wrapped in its own try/except, the same "never raise" contract every other
# step in this pipeline follows (see risk.py's assess_risk and the step3_x
# fallback paths): a guardrail/insight agent that can crash the whole run is
# worse than one that's merely conservative for this run.
def run_forecast_agent(records: dict, cfg: PipelineConfig) -> dict:
    if not cfg.capabilities.forecast_costs:
        return {}
    try:
        return forecast_costs(records)
    except Exception as exc:
        log.warning("Cost Forecast agent failed (%s); continuing without a forecast.", exc)
        return {}


def run_tag_governance_agent(records: dict, cfg: PipelineConfig) -> list[dict]:
    if not cfg.capabilities.check_tag_governance:
        return []
    try:
        return check_tag_governance(records)
    except Exception as exc:
        log.warning("Tag Governance agent failed (%s); continuing without tag findings.", exc)
        return []


def run_root_cause_agent(records: dict, signals: list[dict], cfg: PipelineConfig) -> list[dict]:
    if not signals or not cfg.capabilities.explain_anomalies:
        return signals
    try:
        return explain_anomalies(records, signals)
    except Exception as exc:
        log.warning("Anomaly Root-Cause agent failed (%s); keeping anomalies without drivers.", exc)
        return signals


def run_pipeline(cfg: PipelineConfig, on_event: Optional[EventCallback] = None) -> dict:
    tracker = MetadataTracker()
    llm = LLMClient(cfg.llm)
    timings: dict[str, float] = {}

    def _start(step: str) -> float:
        start = time.time()
        timings[step] = start
        _emit(on_event, "step_start", {"step": step, "label": STEP_LABELS[step][0]})
        return start

    def _done(step: str, extra: Optional[dict] = None) -> None:
        elapsed = round(time.time() - timings.get(step, time.time()), 2)
        payload = {"step": step, "label": STEP_LABELS[step][1], "elapsed_s": elapsed}
        if extra:
            payload["info"] = extra
        _emit(on_event, "step_complete", payload)

    _emit(on_event, "run_start", {"query": cfg.user_query, "sources": cfg.sources})

    # --- Input sources ---
    _start("inputs")
    log.info("=== INPUT SOURCES ===")
    sources = build_sources(cfg)
    payloads: list[SourcePayload] = []
    for source in sources:
        if not source.is_available():
            log.warning("Source %s not available; skipping.", source.kind)
            continue
        payload = source.fetch()
        tracker.record_source(
            payload.kind, payload.name, payload.total_records, payload.notes
        )
        payloads.append(payload)
    if not payloads:
        _emit(on_event, "error", {"message": "No input sources produced data."})
        raise RuntimeError("No input sources produced data. Check config and inputs.")
    _done("inputs", {"records": sum(p.total_records for p in payloads), "count": len(payloads)})

    # --- Step 1 ---
    _start("step1")
    log.info("=== STEP 1: SIMPLIFY & NORMALIZE ===")
    tracker.record_stage("step1_normalize")
    step1 = run_step1_normalize(cfg, payloads, llm)
    _done("step1", {"tables": list(step1["records"].keys())})

    # --- Step 2 ---
    _start("step2")
    log.info("=== STEP 2: CONTEXT LOAD ===")
    tracker.record_stage("step2_context_load")
    context = run_step2_context_load(cfg, step1)
    _done("step2", {"total_cost": context["business_metadata"]["total_cost_observed"]})

    # --- Cost Forecast / Tag Governance / Anomaly Root-Cause agents (parallel) ---
    # None of the three depend on each other's output — forecast and tag
    # governance only need context["records"], root-cause only needs
    # context["anomaly_signals"] — so they run concurrently on a thread pool
    # instead of one after another (mirrors this module's own "3.1 || 3.2"
    # branch shape, but for real this time). Each writes to its own context
    # key, so there is no shared-state race between the threads. The runner
    # functions themselves (module-level, see above) never raise.
    for key in ("forecast", "tag_governance", "root_cause"):
        _start(key)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(run_forecast_agent, context["records"], cfg): "forecast",
            pool.submit(run_tag_governance_agent, context["records"], cfg): "tag_governance",
            pool.submit(run_root_cause_agent, context["records"], context.get("anomaly_signals") or [], cfg): "root_cause",
        }
        for future in as_completed(futures):
            key = futures[future]
            result = future.result()
            if key == "forecast":
                context["forecast"] = result
                _done("forecast", {"flag": result.get("flag", False)})
            elif key == "tag_governance":
                context["tag_findings"] = result
                _done("tag_governance", {"findings": len(result)})
            else:
                context["anomaly_signals"] = result
                _done("root_cause", {"anomalies": len(result)})

    # --- Step 3.1 ---
    _start("step3_1")
    log.info("=== STEP 3.1: GENERATE CHARTS ===")
    tracker.record_stage("step3_1_charts")
    charts = run_step3_1_charts(cfg, context, llm)
    _done("step3_1", {"charts": len(charts.get("charts", []))})

    # --- Step 3.2 ---
    _start("step3_2")
    log.info("=== STEP 3.2: ANALYSIS & METRICS ===")
    tracker.record_stage("step3_2_analysis")
    analysis = run_step3_2_analysis(cfg, context, llm)
    _done("step3_2", {"anomalies": len(analysis.get("anomalies", []))})

    # --- Step 3.3 ---
    _start("step3_3")
    log.info("=== STEP 3.3: SUMMARY ===")
    tracker.record_stage("step3_3_summary")
    summary = run_step3_3_summary(cfg, context, analysis, llm)
    _done("step3_3", {"key_findings": len(summary.get("key_findings", []))})

    # --- Step 4 ---
    _start("step4")
    log.info("=== STEP 4: COMBINE ===")
    tracker.record_stage("step4_combine")
    combined = run_step4_combine(context, charts, analysis, summary)
    _done("step4")

    # --- Step 5 ---
    _start("step5")
    log.info("=== STEP 5: FINAL RESPONSE ===")
    result = run_step5_finalize(cfg, combined, tracker)
    _done("step5", {"json": result["json_path"], "markdown": result["markdown_path"]})

    # --- Actions: turn recommendations into applyable, stored actions ---
    run_id = Path(result["json_path"]).stem  # e.g. insights_20260604_101500
    actions = plan_actions(run_id, combined, context["records"], backend=cfg.action_backend, cfg=cfg)
    store = ActionStore(cfg.output_folder / "pending_actions.json")
    store.upsert_many(actions)
    result["run_id"] = run_id
    result["actions"] = [a.to_dict() for a in actions]
    # Surface where/how approved changes land so the UI can show the apply gate.
    result["action_backend"] = cfg.action_backend
    result["applied_folder"] = str(cfg.applied_folder)
    _emit(on_event, "actions", {"actions": result["actions"]})

    # --- Push the report card to Teams (if a webhook is configured) ---
    posted = notify_teams(cfg, combined, actions)
    if posted is not None:
        result["teams_posted"] = posted

    _emit(on_event, "final", {"result": result})
    return result
