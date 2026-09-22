"""CLI entry point for the AWS Cost & Ops Insights pipeline.

Examples
--------
    # default: local CSV in docs/
    python main.py

    # switch sources via CLI (CSV is the default; the others are stubs that
    # activate automatically when AWS creds are set in .env)
    python main.py --sources local_csv
    python main.py --sources local_csv cost_explorer cloudwatch

    # supply a focused question instead of the default broad analysis
    python main.py --query "Which service drove the biggest cost spike?"
"""

from __future__ import annotations

import argparse
import json
import sys

# Windows consoles default to cp1252 and choke on LLM output that contains
# Unicode (≈, →, en-dashes, etc). Force UTF-8 on stdout/stderr.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from src.actions import ALL, APPLY, QUIT, SKIP, Action, review_and_apply
from src.config import load_config
from src.orchestrator import run_pipeline


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AWS Cost & Ops Insights pipeline")
    parser.add_argument(
        "--sources",
        nargs="+",
        default=["local_csv"],
        choices=["local_csv", "cost_explorer", "cloudwatch"],
        help="Input sources to use. Defaults to local_csv only.",
    )
    parser.add_argument(
        "--query",
        default="Provide a comprehensive AWS cost analysis with key insights and recommendations.",
        help="User query that focuses the analysis and summary steps.",
    )
    parser.add_argument(
        "--docs-folder",
        default=None,
        help="Override the docs folder for the local_csv source.",
    )
    parser.add_argument(
        "--notify-teams",
        action="store_true",
        help="Push the report card to Teams (requires TEAMS_WEBHOOK_URL in .env).",
    )
    parser.add_argument(
        "--action-backend",
        choices=["csv", "aws"],
        default=None,
        help="Executor for applied recommendations. csv (default) writes updated "
        "rows to outputs/applied/<run>/; aws is the (stubbed) live-API path.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="After the report, review recommendations and apply approved ones. "
        "On by default when run in a terminal.",
    )
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="Never enter the apply/review step (just list recommendations).",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Auto-approve every recommended change (no prompts). Implies --apply.",
    )
    return parser.parse_args()


def _interactive_decide(action: Action) -> str:
    """Ask the operator whether to apply one recommendation (Claude-Code style)."""
    print()
    print(f"  Recommendation: {action.title}")
    print(f"    impact : {action.impact}")
    print(f"    target : {action.table}.csv  [{action.match_column} = {action.match_value}]")
    print(f"    writes : {action.set_fields}")
    while True:
        ans = input("  Apply this change? [y]es / [n]o / [a]ll / [q]uit: ").strip().lower()
        if ans in ("y", "yes"):
            return APPLY
        if ans in ("n", "no", ""):
            return SKIP
        if ans in ("a", "all"):
            return ALL
        if ans in ("q", "quit"):
            return QUIT
        print("  Please answer y / n / a / q.")


def main() -> int:
    args = _parse_args()
    overrides = {"sources": args.sources, "user_query": args.query}
    if args.docs_folder:
        from pathlib import Path

        overrides["docs_folder"] = Path(args.docs_folder)
    if args.action_backend:
        overrides["action_backend"] = args.action_backend
    # Teams push is opt-in from the CLI: suppress it unless --notify-teams.
    if not args.notify_teams:
        overrides["teams_webhook_url"] = ""
    cfg = load_config(**overrides)

    result = run_pipeline(cfg)

    print("\n=== PIPELINE COMPLETE ===")
    print(f"JSON output:     {result['json_path']}")
    print(f"Markdown output: {result['markdown_path']}")

    actions = result.get("actions", [])
    if actions:
        executable = sum(1 for a in actions if a.get("executable"))
        print(f"\nActions planned: {len(actions)} ({executable} applyable)")
        for a in actions:
            tag = "[applyable]" if a.get("executable") else "[manual]   "
            print(f"  {tag} {a.get('title')}")
        if "teams_posted" in result:
            print(f"\nTeams card posted: {result['teams_posted']}")
        else:
            print("\nTeams not notified (use --notify-teams + TEAMS_WEBHOOK_URL).")
    summary = result["payload"].get("summary", {})
    if summary.get("key_findings"):
        print("\nKey Findings:")
        for f in summary["key_findings"]:
            print(f"  - {f}")
    else:
        print("\n(No key findings returned by the summary step.)")
    print()
    print(json.dumps(result["payload"].get("analysis", {}).get("kpis", {}), indent=2, default=str))

    # --- Human-in-the-loop: ask before applying the recommended changes ---
    do_review = args.yes or args.apply or (sys.stdin.isatty() and not args.no_apply)
    action_objs = [Action.from_dict(a) for a in result.get("actions", [])]
    has_executable = any(a.executable and a.status == "pending" for a in action_objs)
    if do_review and has_executable:
        print("\n=== APPLY RECOMMENDED CHANGES ===")
        print(
            f"Backend: {cfg.action_backend} "
            f"(approved changes are written to {cfg.applied_folder})"
        )
        decide = (lambda _a: APPLY) if args.yes else _interactive_decide
        review_and_apply(cfg, action_objs, decide)
    elif has_executable:
        print("\n(Recommendations not applied. Re-run with --apply to review them.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
