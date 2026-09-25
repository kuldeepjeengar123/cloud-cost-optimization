"""LangGraph-based pipeline orchestrator — built alongside ``orchestrator.py``,
not yet wired into ``server.py``/``main.py`` (see the note at the bottom of
this file for how to switch over).

Same pipeline, same behavior, same frontend SSE contract as
``orchestrator.run_pipeline`` — every ``STEP_LABELS`` key, every
``step_start``/``step_complete`` event shape, every post-graph action
(``plan_actions``, Teams notification) is identical. What changes is *how*
the steps run: a ``langgraph.graph.StateGraph`` instead of straight-line
Python, with the Cost Forecast / Tag Governance / Anomaly Root-Cause agents
expressed as a real parallel fan-out (three edges from one node) instead of
a hand-rolled ``ThreadPoolExecutor`` block.

Two API gotchas found by hands-on testing against the installed
``langgraph==0.2.76`` (its API has changed across versions, so don't assume
shape from memory):

1. A node name may not equal a declared ``PipelineState`` key —
   ``StateGraph.add_node`` raises ``ValueError`` if it does. Every node below
   is named differently from the state key(s) it writes (e.g. node
   ``forecast_agent`` writes state key ``forecast``).
2. ``compiled.invoke({})`` with a fully empty dict raises
   ``InvalidUpdateError`` — the initial state must contain at least one real
   key, hence the ``{"payloads": []}`` seed in ``run_pipeline_graph`` (
   immediately overwritten by ``load_inputs``).

A third rule that shaped this module's structure: LangGraph disallows two
nodes in the *same superstep* writing to the same state key unless a reducer
is configured for it (confirmed empirically). ``forecast_agent`` /
``tag_governance_agent`` / ``root_cause_agent`` run in parallel, so each
writes only to its own dedicated key (``forecast`` / ``tag_findings`` /
``agent_anomaly_signals``) rather than all three trying to update a shared
``context`` dict — the merge back into a single ``context``-shaped dict (the
shape ``run_step3_2_analysis`` expects) happens once, sequentially, inside
``analyze_metrics`` after the fan-in.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .actions import ActionStore, plan_actions
from .capabilities.metadata import MetadataTracker
from .config import PipelineConfig
from .inputs import build_sources
from .inputs.base import SourcePayload
from .integrations.teams import notify_teams
from .llm.client import LLMClient
from .orchestrator import (
    EventCallback,
    STEP_LABELS,
    run_forecast_agent,
    run_root_cause_agent,
    run_tag_governance_agent,
)
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

log = get_logger("orchestrator_graph")


class PipelineState(TypedDict, total=False):
    payloads: list[SourcePayload]
    step1: dict
    context: dict
    forecast: dict
    tag_findings: list[dict]
    agent_anomaly_signals: list[dict]
    charts: dict
    analysis: dict
    summary: dict
    combined: dict
    result: dict


def _emit(on_event: Optional[EventCallback], kind: str, payload: dict) -> None:
    if on_event is not None:
        try:
            on_event(kind, payload)
        except Exception as exc:  # frontend hiccups must never break the pipeline
            log.warning("on_event raised: %s", exc)


def build_graph(
    cfg: PipelineConfig,
    llm: LLMClient,
    tracker: MetadataTracker,
    on_event: Optional[EventCallback] = None,
):
    """Build and compile the pipeline graph. No checkpointer is passed to
    ``.compile()`` in this pass — see the module docstring in
    ``orchestrator.py`` for the follow-up plan."""
    timings: dict[str, float] = {}

    def _start(step: str) -> None:
        timings[step] = time.time()
        _emit(on_event, "step_start", {"step": step, "label": STEP_LABELS[step][0]})

    def _done(step: str, extra: Optional[dict] = None) -> None:
        elapsed = round(time.time() - timings.get(step, time.time()), 2)
        payload = {"step": step, "label": STEP_LABELS[step][1], "elapsed_s": elapsed}
        if extra:
            payload["info"] = extra
        _emit(on_event, "step_complete", payload)

    def load_inputs(state: PipelineState) -> dict:
        _start("inputs")
        log.info("=== INPUT SOURCES ===")
        sources = build_sources(cfg)
        payloads: list[SourcePayload] = []
        for source in sources:
            if not source.is_available():
                log.warning("Source %s not available; skipping.", source.kind)
                continue
            payload = source.fetch()
            tracker.record_source(payload.kind, payload.name, payload.total_records, payload.notes)
            payloads.append(payload)
        if not payloads:
            _emit(on_event, "error", {"message": "No input sources produced data."})
            raise RuntimeError("No input sources produced data. Check config and inputs.")
        _done("inputs", {"records": sum(p.total_records for p in payloads), "count": len(payloads)})
        return {"payloads": payloads}

    def simplify_normalize(state: PipelineState) -> dict:
        _start("step1")
        log.info("=== STEP 1: SIMPLIFY & NORMALIZE ===")
        tracker.record_stage("step1_normalize")
        step1 = run_step1_normalize(cfg, state["payloads"], llm)
        _done("step1", {"tables": list(step1["records"].keys())})
        return {"step1": step1}

    def load_context(state: PipelineState) -> dict:
        _start("step2")
        log.info("=== STEP 2: CONTEXT LOAD ===")
        tracker.record_stage("step2_context_load")
        context = run_step2_context_load(cfg, state["step1"])
        _done("step2", {"total_cost": context["business_metadata"]["total_cost_observed"]})
        return {"context": context}

    # --- Parallel fan-out: each writes only its own state key (see module
    # docstring's third rule) — none of the three touch "context". ---
    def forecast_agent(state: PipelineState) -> dict:
        _start("forecast")
        result = run_forecast_agent(state["context"]["records"], cfg)
        _done("forecast", {"flag": result.get("flag", False)})
        return {"forecast": result}

    def tag_governance_agent(state: PipelineState) -> dict:
        _start("tag_governance")
        result = run_tag_governance_agent(state["context"]["records"], cfg)
        _done("tag_governance", {"findings": len(result)})
        return {"tag_findings": result}

    def root_cause_agent(state: PipelineState) -> dict:
        _start("root_cause")
        signals = state["context"].get("anomaly_signals") or []
        result = run_root_cause_agent(state["context"]["records"], signals, cfg)
        _done("root_cause", {"anomalies": len(result)})
        return {"agent_anomaly_signals": result}

    def generate_charts(state: PipelineState) -> dict:
        # Doesn't need forecast/tag_findings/root-cause output (its prompt
        # never references them — see step3_1_charts.py's _prompt()), so it
        # reads the pre-agent context as-is.
        _start("step3_1")
        log.info("=== STEP 3.1: GENERATE CHARTS ===")
        tracker.record_stage("step3_1_charts")
        charts = run_step3_1_charts(cfg, state["context"], llm)
        _done("step3_1", {"charts": len(charts.get("charts", []))})
        return {"charts": charts}

    def analyze_metrics(state: PipelineState) -> dict:
        # The one place the three agents' separate keys get folded back into
        # a single context dict — the shape run_step3_2_analysis (and every
        # step after it) expects, matching orchestrator.py's in-place
        # ``context["forecast"] = ...`` mutations exactly.
        _start("step3_2")
        log.info("=== STEP 3.2: ANALYSIS & METRICS ===")
        tracker.record_stage("step3_2_analysis")
        context = {
            **state["context"],
            "forecast": state.get("forecast", {}),
            "tag_findings": state.get("tag_findings", []),
            "anomaly_signals": state.get("agent_anomaly_signals") or state["context"].get("anomaly_signals", []),
        }
        analysis = run_step3_2_analysis(cfg, context, llm)
        _done("step3_2", {"anomalies": len(analysis.get("anomalies", []))})
        return {"analysis": analysis, "context": context}

    def write_summary(state: PipelineState) -> dict:
        _start("step3_3")
        log.info("=== STEP 3.3: SUMMARY ===")
        tracker.record_stage("step3_3_summary")
        summary = run_step3_3_summary(cfg, state["context"], state["analysis"], llm)
        _done("step3_3", {"key_findings": len(summary.get("key_findings", []))})
        return {"summary": summary}

    def combine_all(state: PipelineState) -> dict:
        _start("step4")
        log.info("=== STEP 4: COMBINE ===")
        tracker.record_stage("step4_combine")
        combined = run_step4_combine(state["context"], state["charts"], state["analysis"], state["summary"])
        _done("step4")
        return {"combined": combined}

    def finalize(state: PipelineState) -> dict:
        _start("step5")
        log.info("=== STEP 5: FINAL RESPONSE ===")
        result = run_step5_finalize(cfg, state["combined"], tracker)
        _done("step5", {"json": result["json_path"], "markdown": result["markdown_path"]})
        return {"result": result}

    graph = StateGraph(PipelineState)
    for name, fn in [
        ("load_inputs", load_inputs),
        ("simplify_normalize", simplify_normalize),
        ("load_context", load_context),
        ("forecast_agent", forecast_agent),
        ("tag_governance_agent", tag_governance_agent),
        ("root_cause_agent", root_cause_agent),
        ("generate_charts", generate_charts),
        ("analyze_metrics", analyze_metrics),
        ("write_summary", write_summary),
        ("combine_all", combine_all),
        ("finalize", finalize),
    ]:
        graph.add_node(name, fn)

    graph.add_edge(START, "load_inputs")
    graph.add_edge("load_inputs", "simplify_normalize")
    graph.add_edge("simplify_normalize", "load_context")
    # Parallel fan-out (replaces orchestrator.py's ThreadPoolExecutor block) —
    # generate_charts waits for all three, exactly like today's sequential
    # code (step3_1 runs only after the agents complete).
    graph.add_edge("load_context", "forecast_agent")
    graph.add_edge("load_context", "tag_governance_agent")
    graph.add_edge("load_context", "root_cause_agent")
    graph.add_edge("forecast_agent", "generate_charts")
    graph.add_edge("tag_governance_agent", "generate_charts")
    graph.add_edge("root_cause_agent", "generate_charts")
    graph.add_edge("generate_charts", "analyze_metrics")
    graph.add_edge("analyze_metrics", "write_summary")
    graph.add_edge("write_summary", "combine_all")
    graph.add_edge("combine_all", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


def run_pipeline_graph(cfg: PipelineConfig, on_event: Optional[EventCallback] = None) -> dict:
    """Drop-in alternative to ``orchestrator.run_pipeline`` — same signature,
    same return shape, same SSE event contract. Not yet wired into
    ``server.py``/``main.py``; to switch over, change their import from::

        from .orchestrator import run_pipeline

    to::

        from .orchestrator_graph import run_pipeline_graph as run_pipeline
    """
    tracker = MetadataTracker()
    llm = LLMClient(cfg.llm)

    _emit(on_event, "run_start", {"query": cfg.user_query, "sources": cfg.sources})

    compiled = build_graph(cfg, llm, tracker, on_event)
    # Non-empty seed required (see module docstring, gotcha 2) — immediately
    # overwritten by load_inputs.
    final_state = compiled.invoke({"payloads": []})

    context = final_state["context"]
    combined = final_state["combined"]
    result = final_state["result"]

    # --- Actions: turn recommendations into applyable, stored actions ---
    # (identical to orchestrator.run_pipeline from here on)
    run_id = Path(result["json_path"]).stem
    actions = plan_actions(run_id, combined, context["records"], backend=cfg.action_backend, cfg=cfg)
    store = ActionStore(cfg.output_folder / "pending_actions.json")
    store.upsert_many(actions)
    result["run_id"] = run_id
    result["actions"] = [a.to_dict() for a in actions]
    result["action_backend"] = cfg.action_backend
    result["applied_folder"] = str(cfg.applied_folder)
    _emit(on_event, "actions", {"actions": result["actions"]})

    posted = notify_teams(cfg, combined, actions)
    if posted is not None:
        result["teams_posted"] = posted

    _emit(on_event, "final", {"result": result})
    return result
