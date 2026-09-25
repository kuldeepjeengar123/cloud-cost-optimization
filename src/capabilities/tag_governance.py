"""Deterministic tagging-governance check over normalized cost records.

Flags spend that cannot be allocated to a project/owner because the resource
behind it is missing the tag dimension the rest of this project already keys
off (see ``actions.planner``'s entity catalog and ``config.aws_write_require_tag``)
— but until now nothing proactively surfaced *which* resources or how much
spend that affects. Same calling convention as anomaly_detection.py: a pure
function of the normalized records, returns plain findings.
"""

from __future__ import annotations

_UNTAGGED_MARKERS = {"", "none", "null", "unknown", "untagged", "n/a"}
_TAG_COLUMNS = ("project_tag",)  # extend here if a source adds owner/env columns
_HIGH_SEVERITY_SHARE = 0.2  # untagged spend above this share of the table's total


def check_tag_governance(records: dict[str, list[dict]]) -> list[dict]:
    findings: list[dict] = []
    for table, rows in (records or {}).items():
        if not rows:
            continue
        tag_col = next((c for c in _TAG_COLUMNS if c in rows[0]), None)
        if tag_col is None:
            continue

        table_total = sum(
            r.get("cost", 0) for r in rows if isinstance(r.get("cost"), (int, float))
        )
        exposed_cost, count = 0.0, 0
        for row in rows:
            value = str(row.get(tag_col) or "").strip().lower()
            if value not in _UNTAGGED_MARKERS:
                continue
            count += 1
            cost = row.get("cost")
            if isinstance(cost, (int, float)):
                exposed_cost += float(cost)

        if count == 0:
            continue
        share = exposed_cost / table_total if table_total > 0 else 0.0
        findings.append({
            "finding": f"{count} row(s) in {table} missing {tag_col}",
            "severity": "high" if share > _HIGH_SEVERITY_SHARE else "medium",
            "evidence": f"${exposed_cost:.2f} of spend in {table} "
                        f"({share * 100:.0f}% of that table) cannot be allocated to a "
                        f"project or owner without {tag_col}.",
            "source": "tag_governance",
            "table": table,
            "tag_column": tag_col,
            "rows_affected": count,
            "cost_exposed": round(exposed_cost, 6),
        })
    return findings
