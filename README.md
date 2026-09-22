# AWS Cost & Ops Insights Pipeline

A modular, LLM-augmented pipeline that turns raw AWS cost/ops data into a
structured insight report (KPIs, anomalies, recommendations, chart specs).
It reads from one of three sources with no other code changes:

- **Local CSV** in [docs/](docs/) — the original static source.
- **Mock AWS** — a built-in, **credential-free** simulation of the AWS APIs
  (Cost Explorer / CloudWatch / EC2). It serves AWS-shaped JSON, holds a live
  EC2 fleet you can mutate in real time, and never opens a browser or touches a
  real account. This is the default "AWS" path and is meant for demos.
- **Real AWS** — boto3 against the live APIs (only when `AWS_USE_MOCK=0` and
  credentials are present).

Because the mock returns the same response shape as boto3, switching to real
AWS later is a config change (`AWS_USE_MOCK=0`), not a rewrite. The web
dashboard's header has a **Target** dropdown (Local files / Mock AWS / Real
AWS) that drives this per-run — see "Two-stage, batch-commit approval" below
for why picking Real AWS only changes what you can *read*, never what gets
written.

## Architecture (matches the flow diagram)

```
INPUT SOURCES                            ADDED CAPABILITIES
  - CloudWatch API   (stub)              - Data validation
  - Cost Explorer API (stub)             - Deduplication
  - Local CSV / JSON (ACTIVE)            - Normalization
        |                                - Enrichment
        v                                - Metadata tracking
  STEP 1: Simplify & Normalize (LLM)     - Configurability
        v
  STEP 2: Context Load
        v
  STEP 3.1 Charts (LLM)   STEP 3.2 Analysis (LLM)   STEP 3.3 Summary (LLM)
                          \      |       /
                           v     v      v
                       STEP 4: Combine
                              v
                       STEP 5: Final Response  -> outputs/insights_*.json|md
```

## Directory layout

```
src/
  config.py                  # Pipeline configuration
  orchestrator.py            # Runs the full flow
  orchestrator_graph.py      # LangGraph rebuild of orchestrator.py — same behavior and
                              # SSE contract, built alongside it, not yet wired into
                              # server.py/main.py (see its own docstring for the swap-over)
  inputs/                    # Input source connectors
    base.py
    local_csv.py             # ACTIVE
    cost_explorer_api.py     # mock (default) or boto3 (real)
    cloudwatch_api.py        # mock (default) or boto3 (real)
    factory.py
  mock_aws/                  # Credential-free fake AWS (demo)
    pricing.py               # instance-type -> hourly price
    state.py                 # mutable fleet, derives cost tables, persists JSON
    server.py                # AWS-shaped REST server + live console
    client.py                # thin HTTP client (boto3 replacement)
  actions/                   # Human-in-the-loop: recommendations -> apply
    planner.py  executor.py  store.py  review.py  models.py
    approval.py              # employee raises -> RE stages -> RE commits (batch)
    rollback.py              # undo a committed batch (stop->start, resize back, tag restore)
    risk.py                  # blast-radius guardrail on an executable action before approval
  aws/
    real_client.py           # boto3 client, same shape as mock_aws.client — hard dry-run gate
  mcp_server/                # MCP tools for an AI agent (Claude Code/Desktop, etc.)
    server.py  policy.py  audit.py
  pipeline/                  # The 5 pipeline steps
    step1_normalize.py
    step2_context_load.py
    step3_1_charts.py
    step3_2_analysis.py
    step3_3_summary.py
    step4_combine.py
    step5_finalize.py
  capabilities/              # Added capabilities from the diagram
    validation.py
    deduplication.py
    normalization.py
    enrichment.py
    anomaly_detection.py     # day-over-day cost-spike detector
    forecasting.py           # Cost Forecast agent — trend + 30-day projection
    tag_governance.py        # Tag Governance agent — untagged spend exposure
    root_cause.py            # Anomaly Root-Cause agent — correlates a spike's likely driver
    metadata.py
  chat/                      # Chat widget: Q&A grounded in the latest run's insights
    answer.py  memory.py
  llm/
    client.py                # OpenRouter wrapper (streaming + JSON helpers)
  utils/
    logger.py
docs/                        # Local CSV inputs (default source)
outputs/                     # Generated insights + mock_state.json
tests/                       # unittest suite — no LLM/network calls
main.py                      # CLI entry point
server.py                    # Web UI + SSE pipeline server
mock_aws.py                  # Launches the credential-free mock AWS server
mcp_server.py                # Launches the MCP server (stdio)
scripts/
  smoke_test.py              # End-to-end: raise -> stage -> commit -> verify -> rollback
  mcp_smoke_test.py           # Same, through the MCP tool functions + RE-token gate
```

## Quickstart

```bash
# 1. Install dependencies (uv is recommended — see pyproject.toml/uv.lock)
uv sync
# or
pip install python-dotenv requests boto3 mcp redis langgraph langchain-core

# 2. Provide an OpenRouter API key in .env
echo 'OPENROUTER_API_KEY="sk-or-..."' >> .env

# 3. Run with the default local CSV source
python main.py

# 4. Outputs land in outputs/insights_<timestamp>.json and .md
```

## Mock AWS (credential-free, real-time)

A fake AWS that behaves like the real one but needs **no credentials** and
**never opens a browser**. State (an EC2 fleet seeded from the CSVs) lives in
`outputs/mock_state.json` and persists across restarts; every applied change is
reflected the next time costs are read.

You don't have to start anything separately — when you select the Mock AWS
source, the pipeline **auto-starts an embedded mock server** in-process. Run
`python mock_aws.py` only if you want the standalone **live console** in a
browser.

```bash
# Web UI — just pick "Mock AWS (live)" in the source dropdown
python server.py              # http://127.0.0.1:8765   (mock auto-starts)

# CLI — read from the mock and let approved fixes act on it live
python main.py --sources cost_explorer cloudwatch --action-backend aws --apply

# Optional: standalone server for the live browser console
python mock_aws.py            # http://127.0.0.1:8788
```

In the **live console** (open `http://127.0.0.1:8788` in a browser) you can
stop / start / resize / terminate instances and watch the cost totals change.
The agent's "Apply" buttons do the same thing programmatically: e.g. approving
*"rightsize over-provisioned m5.xlarge instances"* resizes them and drops the
fleet cost on the next read.

API surface (all return AWS-shaped JSON):

| Method | Path | Mimics |
| --- | --- | --- |
| GET  | `/aws/ce/cost-and-usage?group_by=SERVICE\|REGION\|INSTANCE_TYPE\|TAG` | `ce.get_cost_and_usage` |
| GET  | `/aws/cw/metric-statistics?namespace=AWS/EC2&metric=CPUUtilization` | `cloudwatch.get_metric_statistics` |
| GET  | `/aws/ec2/instances` | `ec2.describe_instances` |
| POST | `/aws/ec2/instances/<id>/stop\|start\|resize\|terminate\|tags` | `ec2.*` |
| GET/POST | `/aws/budgets` | `budgets.*` |
| POST | `/aws/reset` | re-seed from CSV |

### Switching to real AWS

Set credentials in `.env` and turn the mock off:

```bash
AWS_USE_MOCK=0   # (in .env) — now cost_explorer/cloudwatch use boto3
python main.py --sources local_csv cost_explorer cloudwatch
```

The `CostExplorerSource` / `CloudWatchSource` parse the same response shape in
both modes, so nothing else in the pipeline changes.

### Real AWS write guardrail

Wiring `AWS_USE_MOCK=0` gives you real *reads* immediately. Real *mutations*
(stop/resize/terminate/tag/budget) additionally require
`AWS_ALLOW_REAL_WRITES=1` — until then every write is logged and returned as a
dry-run, never executed:

```
[DRY-RUN] AWS_ALLOW_REAL_WRITES=0 — would call: ec2.stop_instances(InstanceIds=['i-...'])
```

Two more knobs narrow the blast radius even once writes are enabled:
`AWS_WRITE_ALLOWED_REGIONS` (comma-separated allowlist; empty = no region
restriction) and `AWS_WRITE_REQUIRE_TAG` (a tag key every write target must
carry; empty = no tag restriction). The gate lives in
`src/aws/real_client.py`'s `_guard_write()` — every caller (the web dashboard,
the MCP server, anything else) goes through the same check.

### Config knobs (env / `.env`)

| Var | Default | Meaning |
| --- | --- | --- |
| `AWS_USE_MOCK` | `1` | `1` = mock (no creds); `0` = real boto3 |
| `AWS_ENDPOINT_URL` | `http://127.0.0.1:8788` | where the mock server listens |
| `ACTION_BACKEND` | `csv` | `csv` annotates CSVs; `aws` drives the mock/live API |
| `AWS_ALLOW_REAL_WRITES` | `0` | `1` lets real-AWS mutations actually execute (see above) |
| `AWS_WRITE_ALLOWED_REGIONS` | *(empty)* | comma-separated region allowlist for real writes |
| `AWS_WRITE_REQUIRE_TAG` | *(empty)* | tag key a real-write target must carry |
| `RE_TEAM_TOKEN` | *(empty)* | shared secret required (as `X-RE-Token` / `re_token`) to stage, commit, unstage, or roll back |

## Two-stage, batch-commit approval

An employee raises a recommendation; only the RE team can make it real, and
only once the **entire queue** has been triaged:

```
pending --raise--> pending_re --stage(approve)--> staged_approve --\
                        |                                           |
                        \--stage(decline)--> staged_decline --commit_batch--> declined
                                                                     |
                                                      commit_batch --/--> applied/acknowledged
```

- **Staging never touches anything.** `stage_decision()` (`src/actions/approval.py`)
  only records RE intent — approve or decline — for one request.
- **Committing is the only path to AWS or a CSV write.** `commit_batch()`
  refuses outright while any request is still `pending_re`: every open item
  must be staged one way or the other before the batch can run. A per-action
  failure (or a partial failure across several targeted instances) leaves
  that one action `staged_approve` for retry instead of silently marking it
  applied — every other action in the batch still commits.
- **Every committed batch can be undone.** Before running, `commit_batch()`
  snapshots each target's pre-change state to
  `outputs/applied/<batch_id>/rollback.json`. `rollback_batch()` reverses
  whatever's reversible (stop → start, resize → resize back, tag → restore)
  and reports terminated instances as irreversible rather than silently
  skipping them.
- **The web dashboard enforces the same gate a human is held to** —
  `web/finops_approval_prototype.html`'s RE view shows a live commit-readiness
  panel (undecided / staged-approve / staged-decline counts, estimated
  savings) and a "Commit N changes" button that's disabled until the queue is
  empty.
- **`RE_TEAM_TOKEN`** (optional) additionally gates stage/unstage/commit/
  rollback behind a shared secret (`X-RE-Token` header on the HTTP API,
  `re_token` argument on MCP tools) — unset by default for local/demo use.
  This is a shared secret, not real authentication; a production deployment
  needs SSO/IAM-backed role checks in front of this server.

Try the whole flow without the LLM pipeline or a browser:

```bash
python scripts/smoke_test.py
```

## MCP server

`src/mcp_server/server.py` exposes this same workflow as MCP tools, so an
agent (Claude Code, Claude Desktop, or any other MCP client) can read the
fleet/costs/recommendations and drive the approval flow — but it can never
reach AWS by any path a human reviewer doesn't also have to go through.
Reads are open to any caller; every write funnels through
`raise_recommendation` → (RE-only) `stage_recommendation_decision` on every
open request → (RE-only) `commit_approved_changes`. There is deliberately no
standalone "just call AWS" tool, not even for budgets. Every call — allowed
or denied — is appended to `outputs/mcp_audit.jsonl`.

```bash
python mcp_server.py            # stdio transport
python scripts/mcp_smoke_test.py  # exercises every tool + the RE-token gate
```

`.mcp.json` at the repo root points Claude Code at it automatically
(`command: .venv/Scripts/python.exe` — on macOS/Linux use `.venv/bin/python`).

## Customizing the analysis

```bash
python main.py --query "Which service drove the biggest cost spike this month?"
python main.py --docs-folder /path/to/other/csvs
```

