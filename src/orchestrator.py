"""Pipeline orchestrator — runs the full flow from inputs to final response.

Mirrors the diagram exactly:
    Inputs -> Step 1 -> Step 2 -> (5 parallel agents -> 3.3) -> Step 4 -> Step 5

Step 3.1 (Charts) and the old, single "Step 3.2: Analysis" LLM call have
been replaced by five narrowly-scoped agents that run concurrently:
run_step3_1_charts, run_cost_anomaly, run_budget_forecast,
run_optimisation_recommendation (all "capable tier" — cfg.llm.model_analysis)
and run_usage_report ("cheap tier" — cfg.llm.model_cheap). Each reads only
``context`` (never another agent's output) and none is needed until Step
3.3/Step 4 afterward, so there's no reason for any of the five to wait on
another — same independence the forecast/tag_governance/root_cause agents
just above them already have.

An optional ``on_event`` callback receives ``(event_kind, payload)`` at every
step boundary so a frontend can stream progress to the user.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from .actions import ActionStore, plan_actions
from .capabilities.forecasting import daily_totals, forecast_costs
from .capabilities.metadata import MetadataTracker
from .capabilities.root_cause import candidate_drivers_for, explain_anomalies
from .capabilities.tag_governance import check_tag_governance, tag_exposure_snapshot
from .config import PipelineConfig
from .inputs import build_sources
from .inputs.base import SourcePayload
from .integrations.teams import notify_teams
from .llm.client import LLMClient
from .prompts import load_prompt
from .pipeline import (
    run_budget_forecast,
    run_cost_anomaly,
    run_optimisation_recommendation,
    run_step1_normalize,
    run_step2_context_load,
    run_step3_1_charts,
    run_step3_3_summary,
    run_step4_combine,
    run_step5_finalize,
    run_usage_report,
)
# Reused from the (still-active-for-orchestrator_graph.py) old Step 3.2 —
# KPIs are deterministic, so there's no reason to recompute this logic.
from .pipeline.step3_2_analysis import _baseline_kpis
from .utils.logger import get_logger

log = get_logger("orchestrator")

EventCallback = Callable[[str, dict], None]

# Friendly labels surfaced to the frontend. The verbs that animate in the UI
# are picked client-side so we don't have to coordinate timing here.
STEP_LABELS = {
    "inputs":         ("Fetching cost data from AWS",     "Cost data fetched"),
    "step1":          ("Simplifying & normalizing",      "Normalized"),
    "step2":          ("Loading context & correlations", "Context loaded"),
    "forecast":       ("Forecasting cost trend",         "Forecast ready"),
    "tag_governance": ("Checking tag governance",        "Tags checked"),
    "root_cause":     ("Correlating anomaly root cause",  "Root cause ready"),
    "step3_1":        ("Generating chart specs",         "Charts drafted"),
    # Kept for orchestrator_graph.py, which still uses the single-call Step
    # 3.2 (run_step3_2_analysis) unchanged — this active orchestrator uses
    # the four parallel agents below in its place instead.
    "step3_2":        ("Crunching analysis & metrics",   "Analysis ready"),
    "run_cost_anomaly":                ("Running cost anomaly agent (capable tier)",                "Cost anomaly ready"),
    "run_budget_forecast":             ("Running budget forecast agent (capable tier)",             "Budget forecast ready"),
    "run_optimisation_recommendation": ("Running optimisation recommendation agent (capable tier)", "Optimisation recommendation ready"),
    "run_usage_report":                ("Running usage report agent (cheap tier)",                  "Usage report ready"),
    "step3_3":        ("Writing executive summary",      "Summary written"),
    "step4":          ("Combining all responses",        "Combined"),
    "step5":          ("Finalizing report",              "Report ready"),
    "finalize_actions": ("Planning & risk-assessing recommendations", "Recommendations ready"),
}


def _emit(on_event: Optional[EventCallback], kind: str, payload: dict) -> None:
    if on_event is not None:
        try:
            on_event(kind, payload)
        except Exception as exc:  # frontend hiccups must never break the pipeline
            log.warning("on_event raised: %s", exc)


FORECAST_SYSTEM_PROMPT = load_prompt("forecast_costs")
TAG_GOVERNANCE_SYSTEM_PROMPT = load_prompt("tag_governance")
ROOT_CAUSE_SYSTEM_PROMPT = load_prompt("root_cause")


def _llm_forecast_costs(records: dict, llm: LLMClient, cfg: PipelineConfig) -> dict:
    totals = daily_totals(records)
    if len(totals) < 2:
        return {}
    series = sorted(totals.items())  # [(date, cost), ...] chronological
    user_prompt = (
        "Daily total account spend, oldest first (untrusted data):\n"
        + json.dumps(series, default=str, indent=2)
    )
    raw = llm.complete(system=FORECAST_SYSTEM_PROMPT, user=user_prompt, model=cfg.llm.model_analysis, max_tokens=600)
    parsed = LLMClient.extract_json(raw)
    if not isinstance(parsed, dict):
        raise ValueError("LLM forecast returned no usable JSON")
    if parsed and "flag" not in parsed:
        raise ValueError("LLM forecast response missing 'flag'")
    return parsed


def _llm_check_tag_governance(records: dict, llm: LLMClient, cfg: PipelineConfig) -> list[dict]:
    snapshot = tag_exposure_snapshot(records)
    if not snapshot:
        return []
    user_prompt = (
        "Untagged-spend exposure per table (untrusted data):\n"
        + json.dumps(snapshot, default=str, indent=2)
    )
    raw = llm.complete(system=TAG_GOVERNANCE_SYSTEM_PROMPT, user=user_prompt, model=cfg.llm.model_analysis, max_tokens=1000)
    parsed = LLMClient.extract_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
        raise ValueError("LLM tag governance returned no usable JSON")
    findings = []
    for item in parsed["findings"]:
        if not isinstance(item, dict) or not item.get("finding"):
            continue
        severity = item.get("severity")
        findings.append({
            "finding": str(item["finding"]),
            "severity": severity if severity in ("low", "medium", "high") else "medium",
            "evidence": str(item.get("evidence") or ""),
            "source": "llm",
            "table": item.get("table"),
            "tag_column": item.get("tag_column"),
            "rows_affected": item.get("rows_affected"),
            "cost_exposed": item.get("cost_exposed"),
        })
    return findings


def _llm_explain_anomalies(records: dict, signals: list[dict], llm: LLMClient, cfg: PipelineConfig) -> list[dict]:
    candidates: dict[str, list[dict]] = {}
    for finding in signals:
        table, date = finding.get("table"), finding.get("date")
        rows = (records or {}).get(table) or []
        if not table or not date or not rows:
            continue
        found = candidate_drivers_for(rows, date, finding.get("dimension"))
        if found:
            candidates[finding["finding"]] = found
    if not candidates:
        return signals

    user_prompt = (
        "Anomalies and, for each, every other dimension value present on its date "
        "with its cost that day vs. its own recent average (untrusted data):\n"
        + json.dumps(candidates, default=str, indent=2)
    )
    raw = llm.complete(system=ROOT_CAUSE_SYSTEM_PROMPT, user=user_prompt, model=cfg.llm.model_analysis, max_tokens=1200)
    parsed = LLMClient.extract_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("explained"), list):
        raise ValueError("LLM root cause returned no usable JSON")

    drivers_by_finding: dict[str, list[dict]] = {}
    for item in parsed["explained"]:
        if not isinstance(item, dict) or not item.get("finding"):
            continue
        drivers = [d for d in (item.get("drivers") or []) if isinstance(d, dict) and d.get("dimension") and d.get("value")]
        if drivers:
            drivers_by_finding[str(item["finding"])] = drivers

    explained = []
    for finding in signals:
        drivers = drivers_by_finding.get(finding.get("finding"))
        if not drivers:
            explained.append(finding)
            continue
        updated = dict(finding)
        updated["drivers"] = drivers
        lead = ", ".join(f"{d['value']} ({d['dimension']}, +{d.get('pct_above_own_avg', '?')}%)" for d in drivers)
        updated["evidence"] = f"{finding.get('evidence', '')} Coincided with a jump in: {lead}."
        explained.append(updated)
    return explained


# Module-level (not closures) so they're independently unit-testable — each
# wrapped in its own try/except, the same "never raise" contract every other
# step in this pipeline follows (see risk.py's assess_risk and the step3_x
# fallback paths): a guardrail/insight agent that can crash the whole run is
# worse than one that's merely conservative for this run. Each tries an LLM
# call first (the model judges what's worth flagging, no fixed threshold) and
# only falls back to the deterministic capabilities/ function on a failed or
# unusable call — never on a clean "nothing here" verdict from the LLM.
def run_forecast_agent(records: dict, llm: LLMClient, cfg: PipelineConfig) -> dict:
    if not cfg.capabilities.forecast_costs:
        return {}
    try:
        return _llm_forecast_costs(records, llm, cfg)
    except Exception as exc:
        log.warning("LLM cost forecast failed (%s); using the deterministic forecast.", exc)
    try:
        return forecast_costs(records)
    except Exception as exc:
        log.warning("Deterministic cost forecast also failed (%s); continuing without a forecast.", exc)
        return {}


def run_tag_governance_agent(records: dict, llm: LLMClient, cfg: PipelineConfig) -> list[dict]:
    if not cfg.capabilities.check_tag_governance:
        return []
    try:
        return _llm_check_tag_governance(records, llm, cfg)
    except Exception as exc:
        log.warning("LLM tag governance failed (%s); using the deterministic check.", exc)
    try:
        return check_tag_governance(records)
    except Exception as exc:
        log.warning("Deterministic tag governance also failed (%s); continuing without tag findings.", exc)
        return []


def run_root_cause_agent(records: dict, signals: list[dict], llm: LLMClient, cfg: PipelineConfig) -> list[dict]:
    if not signals or not cfg.capabilities.explain_anomalies:
        return signals
    try:
        return _llm_explain_anomalies(records, signals, llm, cfg)
    except Exception as exc:
        log.warning("LLM anomaly root-cause failed (%s); using the deterministic correlation.", exc)
    try:
        return explain_anomalies(records, signals)
    except Exception as exc:
        log.warning("Deterministic anomaly root-cause also failed (%s); keeping anomalies without drivers.", exc)
        return signals


def run_pipeline(
    cfg: PipelineConfig, on_event: Optional[EventCallback] = None, skip_action_planning: bool = False
) -> dict:
    """``skip_action_planning=True`` runs every analysis agent (normalize,
    context load, charts, analysis, summary, finalize) exactly as usual but
    never calls ``plan_actions()`` or touches ``ActionStore`` — for a target
    that already gets its recommendations from somewhere else (e.g. the "EC2
    Rightsizing" target's live AWS fleet scan) and only wants this run's
    analysis/insights, not a second, competing set of recommendations."""
    tracker = MetadataTracker()
    llm = LLMClient(cfg.llm)
    timings: dict[str, float] = {}

    def _start(step: str) -> float:
        start = time.time()
        timings[step] = start
        _emit(on_event, "step_start", {"step": step, "label": STEP_LABELS[step][0]})
        return start

    def _done(step: str, extra: Optional[dict] = None, data: Optional[dict] = None) -> None:
        elapsed = round(time.time() - timings.get(step, time.time()), 2)
        payload = {"step": step, "label": STEP_LABELS[step][1], "elapsed_s": elapsed}
        if extra:
            payload["info"] = extra
        if data is not None:
            # The step's actual output (the real chart specs, analysis
            # findings, summary text, ...) — not just a count. `info` above
            # stays small on purpose (it also goes out over the SSE stream to
            # the browser on every run); `data` is what a caller like
            # server.py's Postgres logging persists as that step's full
            # response instead of a bare `{"charts": 5}`-style summary.
            payload["data"] = data
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
    _done(
        "inputs", {"records": sum(p.total_records for p in payloads), "count": len(payloads)},
        data={"sources": [{"kind": p.kind, "name": p.name, "total_records": p.total_records, "notes": p.notes} for p in payloads]},
    )

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
    context = run_step2_context_load(cfg, step1, llm)
    _done(
        "step2", {"total_cost": context["business_metadata"]["total_cost_observed"]},
        # Everything context load actually produced, except "records" — the
        # normalized row-level data, already summarized by Step 1's own row.
        data={k: v for k, v in context.items() if k != "records"},
    )

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
            pool.submit(run_forecast_agent, context["records"], llm, cfg): "forecast",
            pool.submit(run_tag_governance_agent, context["records"], llm, cfg): "tag_governance",
            pool.submit(run_root_cause_agent, context["records"], context.get("anomaly_signals") or [], llm, cfg): "root_cause",
        }
        for future in as_completed(futures):
            key = futures[future]
            result = future.result()
            if key == "forecast":
                context["forecast"] = result
                _done("forecast", {"flag": result.get("flag", False)}, data=result)
            elif key == "tag_governance":
                context["tag_findings"] = result
                _done("tag_governance", {"findings": len(result)}, data={"tag_findings": result})
            else:
                context["anomaly_signals"] = result
                _done("root_cause", {"anomalies": len(result)}, data={"anomalies": result})

    # --- Step 3.1 (Charts) + Step 3.2 replacement: 5 parallel agents ---
    # Charts and the four new analysis agents (cost anomaly / budget
    # forecast / optimisation recommendation — capable tier; usage report —
    # cheap tier) are all independent of each other: each reads only
    # `context` (plus, for optimisation, the deterministic `kpis` below),
    # never another one's output, and none of the five is needed until
    # Step 4/3.3 afterward. Charts used to run to completion *before* this
    # block even started, for no reason other than keeping its original
    # position — now all five run on one thread pool together.
    log.info("=== STEP 3.1 + STEP 3.2 REPLACEMENT: PARALLEL AGENTS ===")
    tracker.record_stage("step3_1_charts")
    tracker.record_stage("run_cost_anomaly")
    tracker.record_stage("run_budget_forecast")
    tracker.record_stage("run_optimisation_recommendation")
    tracker.record_stage("run_usage_report")
    kpis = _baseline_kpis(context)
    for key in ("step3_1", "run_cost_anomaly", "run_budget_forecast", "run_optimisation_recommendation", "run_usage_report"):
        _start(key)

    agent_results: dict[str, dict] = {}
    charts: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(run_step3_1_charts, cfg, context, llm): "step3_1",
            pool.submit(run_cost_anomaly, cfg, context, llm): "run_cost_anomaly",
            pool.submit(run_budget_forecast, cfg, context, llm): "run_budget_forecast",
            pool.submit(run_optimisation_recommendation, cfg, context, kpis, llm): "run_optimisation_recommendation",
            pool.submit(run_usage_report, cfg, context, llm): "run_usage_report",
        }
        for future in as_completed(futures):
            key = futures[future]
            result = future.result()
            if key == "step3_1":
                charts = result
                _done(key, {"charts": len(result.get("charts", []))}, data=result)
            elif key == "run_cost_anomaly":
                agent_results[key] = result
                _done(key, {"anomalies": len(result.get("anomalies", []))}, data=result)
            elif key == "run_budget_forecast":
                agent_results[key] = result
                _done(key, {"trends": len(result.get("trends", []))}, data=result)
            elif key == "run_optimisation_recommendation":
                agent_results[key] = result
                _done(key, {"recommendations": len(result.get("recommendations", []))}, data=result)
            else:
                agent_results[key] = result
                _done(key, {"highlights": len(result.get("highlights", []))}, data=result)

    analysis = {
        "kpis": kpis,
        "anomalies": agent_results["run_cost_anomaly"]["anomalies"],
        "trends": agent_results["run_budget_forecast"]["trends"],
        "benchmarks": agent_results["run_budget_forecast"]["benchmarks"],
        "forecast": context.get("forecast") or {},
        "tag_findings": context.get("tag_findings") or [],
        "optimization_recommendations": agent_results["run_optimisation_recommendation"]["recommendations"],
        "usage_report": agent_results["run_usage_report"],
    }

    # --- Step 3.3 ---
    _start("step3_3")
    log.info("=== STEP 3.3: SUMMARY ===")
    tracker.record_stage("step3_3_summary")
    summary = run_step3_3_summary(cfg, context, analysis, llm)
    _done("step3_3", {"key_findings": len(summary.get("key_findings", []))}, data=summary)

    # --- Step 4 ---
    _start("step4")
    log.info("=== STEP 4: COMBINE ===")
    tracker.record_stage("step4_combine")
    combined = run_step4_combine(context, charts, analysis, summary)
    _done("step4", data=combined)

    # --- Step 5 ---
    _start("step5")
    log.info("=== STEP 5: FINAL RESPONSE ===")
    result = run_step5_finalize(cfg, combined, tracker)
    _done("step5", {"json": result["json_path"], "markdown": result["markdown_path"]})

    run_id = Path(result["json_path"]).stem  # e.g. insights_20260604_101500
    result["run_id"] = run_id
    # Surface where/how approved changes land so the UI can show the apply gate.
    result["action_backend"] = cfg.action_backend
    result["applied_folder"] = str(cfg.applied_folder)

    # --- Finalize: plan + risk-assess recommendations, notify Teams ---
    # Genuinely the slowest part of a run for a large recommendation set —
    # plan_actions() calls assess_risk() once per executable action, each a
    # real LLM call — but until now it ran with no step_start/step_complete
    # around it at all, so the UI looked finished at "Report ready" while
    # this kept going in the background for as long as every risk call took.
    _start("finalize_actions")
    tracker.record_stage("finalize_actions")
    if not skip_action_planning:
        # --- Actions: turn recommendations into applyable, stored actions ---
        actions = plan_actions(run_id, combined, context["records"], backend=cfg.action_backend, cfg=cfg)
        store = ActionStore(cfg.output_folder / "pending_actions.json")
        store.upsert_many(actions)
        result["actions"] = [a.to_dict() for a in actions]
        _emit(on_event, "actions", {"actions": result["actions"]})

        # --- Push the report card to Teams (if a webhook is configured) ---
        posted = notify_teams(cfg, combined, actions)
        if posted is not None:
            result["teams_posted"] = posted
    else:
        result["actions"] = []
    _done("finalize_actions", {"actions": len(result["actions"])})

    _emit(on_event, "final", {"result": result})
    return result
