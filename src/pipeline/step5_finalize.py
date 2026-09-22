"""Step 5 — Final Response.

Renders the combined payload into delivery formats:
- structured JSON (for downstream systems)
- a Markdown report (for humans / executive view)

Writes both to ``outputs/`` along with the metadata snapshot so every run is
auditable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..capabilities.metadata import MetadataTracker, attach_metadata
from ..config import PipelineConfig
from ..utils.logger import get_logger

log = get_logger("pipeline.step5")


def _render_markdown(combined: dict, metadata: dict) -> str:
    lines: list[str] = []
    lines.append("# AWS Cost & Ops Insights\n")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()}_\n")

    sources = metadata.get("sources", [])
    if sources:
        lines.append("## Sources")
        for s in sources:
            lines.append(f"- **{s['kind']}** `{s['name']}` — {s.get('total_records', 0)} records")
        lines.append("")

    bm = combined.get("business_metadata", {})
    if bm:
        lines.append("## Headline Metrics")
        lines.append(f"- Total observed cost: **${bm.get('total_cost_observed', 0):.6f}**")
        for table, count in bm.get("tables", {}).items():
            lines.append(f"- `{table}`: {count} rows")
        lines.append("")

    analysis = combined.get("analysis", {})
    kpis = analysis.get("kpis", {})
    if kpis:
        lines.append("## KPIs")
        for k, v in kpis.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")

    if analysis.get("anomalies"):
        lines.append("## Anomalies")
        for a in analysis["anomalies"]:
            sev = a.get("severity", "?")
            lines.append(f"- _[{sev}]_ {a.get('finding', '')} — {a.get('evidence', '')}")
        lines.append("")

    if analysis.get("trends"):
        lines.append("## Trends")
        for t in analysis["trends"]:
            lines.append(f"- {t.get('observation', '')} ({t.get('metric', '')})")
        lines.append("")

    forecast = analysis.get("forecast") or {}
    if forecast:
        lines.append("## Cost Forecast")
        lines.append(
            f"- Recent daily average **${forecast.get('recent_period_daily_avg', 0):.2f}** "
            f"vs. prior **${forecast.get('prior_period_daily_avg', 0):.2f}** "
            f"({forecast.get('trend_pct', 0):+d}%)"
        )
        lines.append(f"- Projected next 30 days: **${forecast.get('projected_30d_total', 0):,.2f}**")
        lines.append("")

    if analysis.get("tag_findings"):
        lines.append("## Tag Governance")
        for tf in analysis["tag_findings"]:
            sev = tf.get("severity", "?")
            lines.append(f"- _[{sev}]_ {tf.get('finding', '')} — {tf.get('evidence', '')}")
        lines.append("")

    summary = combined.get("summary", {})
    if summary.get("key_findings"):
        lines.append("## Key Findings")
        for f in summary["key_findings"]:
            lines.append(f"- {f}")
        lines.append("")

    if summary.get("recommendations"):
        lines.append("## Recommendations")
        for r in summary["recommendations"]:
            lines.append(f"- _[{r.get('impact', '?')}]_ {r.get('action', '')}")
        lines.append("")

    if summary.get("next_steps"):
        lines.append("## Next Steps")
        for ns in summary["next_steps"]:
            lines.append(f"- {ns}")
        lines.append("")

    charts = combined.get("charts", [])
    if charts:
        lines.append("## Suggested Charts")
        for c in charts:
            lines.append(
                f"- **{c.get('title', c.get('id'))}** "
                f"(type: {c.get('type')}, x: {c.get('x')}, y: {c.get('y')}, "
                f"data: `{c.get('data_table')}`)"
            )
        lines.append("")

    return "\n".join(lines)


def run_step5_finalize(
    cfg: PipelineConfig, combined: dict, tracker: MetadataTracker
) -> dict:
    log.info("Step 5: finalize & persist")
    tracker.record_stage("step5_finalize")
    metadata = tracker.snapshot()
    final_payload = attach_metadata(combined, tracker)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir: Path = cfg.output_folder
    json_path = out_dir / f"insights_{timestamp}.json"
    md_path = out_dir / f"insights_{timestamp}.md"

    json_path.write_text(json.dumps(final_payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(combined, metadata), encoding="utf-8")

    log.info("Wrote %s and %s", json_path.name, md_path.name)
    return {
        "payload": final_payload,
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }
