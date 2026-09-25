# Agent Module (M2) — Week 1: LangChain Fundamentals

Runs the same cost-analysis prompt through two independently configurable LLM
clients — both routed through OpenRouter, pointed at two different models —
using LangChain's pipe operator and structured output, and writes a
side-by-side comparison.

## Folder Structure

- `agent_module/schemas.py` — `CostAnalysisResult` Pydantic output schema.
- `agent_module/providers.py` — config loading + `build_llm_client(name)`, a
  generic OpenRouter-backed `ChatOpenAI` factory.
- `agent_module/chains.py` — prompt template, `prompt | llm.with_structured_output(...)`.
- `agent_module/compare.py` — runs every configured client on one cost row, timing each call.
- `agent_module/main.py` — CLI entry point.
- `agent_module/config.json` — the list of configured clients (model, base_url, timeout each).
- `sample_data/sample_cost_row.json` — one example AWS Cost Explorer line item.
- `run_compare.py` — top-level script wrapper.

## Configurable client structure

`config.json` defines an arbitrary number of clients under `"clients"`:

```json
{
  "temperature": 0.2,
  "clients": {
    "openai_gpt": {
      "model": "openai/gpt-4o-mini",
      "base_url": "https://openrouter.ai/api/v1",
      "timeout": 120
    },
    "anthropic_claude": {
      "model": "anthropic/claude-3.5-sonnet",
      "base_url": "https://openrouter.ai/api/v1",
      "timeout": 120
    }
  }
}
```

Each client's `api_key`, `model`, `base_url`, and `timeout` can be overridden
independently via environment variables, without touching code:
`<CLIENT_NAME_UPPER>_API_KEY`, `_MODEL`, `_BASE_URL`, `_TIMEOUT`. If a client
has no override, it falls back to a shared `OPENROUTER_API_KEY`. Adding a
third or fourth client (a different model, or any other OpenAI-compatible
endpoint) only requires a new entry in `config.json` — `compare.py` iterates
over whatever is configured.

## Setup

1. Create a virtual environment and install dependencies:
   - `pip install -r requirements.txt`
2. Copy the env file and fill in real API keys:
   - `.env.example` -> `.env`

## Run

Default:

- `python run_compare.py`

Custom paths:

- `python run_compare.py --input sample_data/sample_cost_row.json --output output/comparison_result.json`

## Output

Writes a JSON report with, per configured client: model name, latency in
seconds, the structured `CostAnalysisResult` (service, spend, currency,
anomaly_flag, summary_sentence), or an error message if that client's call
failed (missing key, bad model id, etc.) — one client's failure never blocks
the others.

## Token Limits and Cost Tracking

Each run also reports real token usage per client, read directly from the
model's own response (`prompt_tokens`, `completion_tokens`, `total_tokens`) —
these are measured, not estimated.

Two additional fields can be set per client in `config.json`, so the report
can also show a token limit and an estimated cost:

- `context_window` — the model's maximum context length, in tokens.
- `pricing.prompt_per_1m_usd` / `pricing.completion_per_1m_usd` — USD price
  per 1,000,000 tokens.

These two values are **not** looked up or guessed automatically, since they
vary by model and change over time. To fill them in accurately, check the
model's own page at https://openrouter.ai/models, which lists both the
context length and the current pricing.

The two clients currently configured use OpenRouter's free tier
(model IDs ending in `:free`), so `pricing` is correctly set to `0` for
both — free-tier calls have no cost. `context_window` is left as `null`
until filled in. If a client is switched to a paid model (for example,
`openai/gpt-4o-mini` or `anthropic/claude-3.5-sonnet`, as in the original
example config above), update its `pricing` and `context_window` values
to see a real estimated cost per call.