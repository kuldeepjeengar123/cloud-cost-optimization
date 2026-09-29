from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .chains import build_cost_analysis_chain
from .providers import build_llm_client, list_client_names, load_config
from .schemas import CostAnalysisResult


@dataclass
class ClientRunResult:
    client: str
    model: str
    latency_seconds: float
    result: Optional[CostAnalysisResult] = None
    error: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    context_window: Optional[int] = None
    estimated_cost_usd: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client": self.client,
            "model": self.model,
            "latency_seconds": round(self.latency_seconds, 3),
            "result": self.result.model_dump() if self.result else None,
            "error": self.error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "context_window": self.context_window,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


def _estimate_cost(
    client_cfg: Dict[str, Any],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> Optional[float]:
    """Estimate USD cost from measured token counts and a client's configured
    per-million-token pricing. Returns None if pricing or token counts are
    not available, rather than guessing a number."""
    pricing = client_cfg.get("pricing") or {}
    prompt_price = pricing.get("prompt_per_1m_usd")
    completion_price = pricing.get("completion_per_1m_usd")
    if prompt_price is None or completion_price is None:
        return None
    if prompt_tokens is None or completion_tokens is None:
        return None
    cost = (prompt_tokens / 1_000_000) * prompt_price + (completion_tokens / 1_000_000) * completion_price
    return round(cost, 6)


def _run_client(
    client_name: str,
    model_name: str,
    llm: Any,
    cost_row: Dict[str, Any],
    client_cfg: Dict[str, Any],
) -> ClientRunResult:
    chain = build_cost_analysis_chain(llm)
    context_window = client_cfg.get("context_window")
    start = time.monotonic()
    try:
        output = chain.invoke({"cost_row_json": json.dumps(cost_row, indent=2)})
        latency = time.monotonic() - start

        raw = output.get("raw")
        parsed = output.get("parsed")
        parsing_error = output.get("parsing_error")

        usage = getattr(raw, "usage_metadata", None) or {}
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        cost = _estimate_cost(client_cfg, prompt_tokens, completion_tokens)

        if parsing_error is not None:
            return ClientRunResult(
                client_name,
                model_name,
                latency,
                error=f"Schema validation failed: {parsing_error}",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                context_window=context_window,
                estimated_cost_usd=cost,
            )

        return ClientRunResult(
            client_name,
            model_name,
            latency,
            result=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            context_window=context_window,
            estimated_cost_usd=cost,
        )
    except Exception as exc:
        return ClientRunResult(
            client_name, model_name, time.monotonic() - start, error=str(exc), context_window=context_window
        )


def run_comparison(cost_row: Dict[str, Any]) -> List[ClientRunResult]:
    """Run the same cost line item through every client configured in config.json."""
    config = load_config()
    results: List[ClientRunResult] = []

    for client_name in list_client_names(config):
        client_cfg = config["clients"][client_name]
        model_name = client_cfg.get("model", "unknown")
        try:
            llm = build_llm_client(client_name, config)
        except RuntimeError as exc:
            results.append(
                ClientRunResult(
                    client_name, model_name, 0.0, error=str(exc), context_window=client_cfg.get("context_window")
                )
            )
            continue
        results.append(_run_client(client_name, model_name, llm, cost_row, client_cfg))
    return results


def load_sample_cost_row(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
