from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from .compare import load_sample_cost_row, run_comparison

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare structured cost-analysis output across configured OpenRouter clients."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("sample_data/sample_cost_row.json"),
        help="Path to a single cost line item JSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/comparison_result.json"),
        help="Path to write the side-by-side comparison JSON.",
    )
    return parser


def main() -> None:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args()

    cost_row = load_sample_cost_row(args.input)
    results = run_comparison(cost_row)

    report = {
        "input_file": str(args.input),
        "cost_row": cost_row,
        "comparisons": [r.to_dict() for r in results],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nCost line item: {json.dumps(cost_row, indent=2)}\n")
    for r in results:
        print("-" * 60)
        print(f"Client   : {r.client} ({r.model})")
        print(f"Latency  : {r.latency_seconds:.3f}s")
        if r.error:
            print(f"Error    : {r.error}")
        else:
            print(f"Result   : {r.result.model_dump()}")
        if r.total_tokens is not None:
            print(f"Tokens   : prompt={r.prompt_tokens} completion={r.completion_tokens} total={r.total_tokens}")
        if r.context_window is not None:
            print(f"Context  : {r.context_window} tokens max (this call used {r.total_tokens or 0})")
        if r.estimated_cost_usd is not None:
            print(f"Est. Cost: ${r.estimated_cost_usd:.6f}")
    print("-" * 60)
    print(f"\nComparison written to: {args.output}")


if __name__ == "__main__":
    main()
