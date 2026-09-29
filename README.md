# AWS Cost & Ops Insights Pipeline

A modular, LLM-augmented pipeline that turns raw AWS cost and operations data
into a structured insight report: key metrics (KPIs), anomalies, cost-saving
recommendations, and chart specifications ready for a dashboard — plus a
human-in-the-loop approval workflow before anything ever changes in AWS.

This document is written for readers who are new to the project. It explains
what the system does, the concepts it relies on, how the pieces fit together,
and how to run it step by step.

## Table of contents

1. [What this project does](#what-this-project-does)
2. [Key concepts for beginners](#key-concepts-for-beginners)
3. [Architecture overview](#architecture-overview)
4. [The 5-step analysis pipeline](#the-5-step-analysis-pipeline)
5. [Data storage: Postgres + Redis](#data-storage-postgres--redis)
6. [Directory layout](#directory-layout)
7. [Prerequisites](#prerequisites)
8. [Installation](#installation)
9. [Running the project](#running-the-project)
10. [Environment variables reference](#environment-variables-reference)
11. [Real AWS access](#real-aws-access)
12. [The approval workflow (human-in-the-loop)](#the-approval-workflow-human-in-the-loop)
13. [The chat widget](#the-chat-widget)
14. [The MCP server (AI agent access)](#the-mcp-server-ai-agent-access)
15. [Testing](#testing)
16. [Troubleshooting / FAQ](#troubleshooting--faq)
17. [Security notes](#security-notes)

---

## What this project does

Every day, a company's AWS bill produces cost data broken down by service,
region, instance type, and project tag. This project reads that data (from a
CSV export, or directly from AWS), asks a Large Language Model (LLM) to
analyze it, and produces:

- A set of **KPIs** (for example, total spend, week-over-week change).
- **Anomalies** (unexpected cost spikes) and a best-guess **root cause**.
- A 30-day **cost forecast**.
- **Tag governance** findings (spend that is not attributed to any project).
- Human-readable **recommendations** (for example, "resize this
  over-provisioned instance") that a person can review and approve before
  anything actually changes in AWS.

Every report, every chat answer, and every approval decision is written to a
durable **Postgres** database, so nothing is lost when the server restarts —
see [Data storage](#data-storage-postgres--redis).

The project can be used in three ways, all built on the same underlying
pipeline:

- As a **command-line tool** (`main.py`) that produces a report file.
- As a **web dashboard** (`server.py`) with a chat interface and an approval
  screen for a "Resource Efficiency" (RE) reviewer.
- As an **MCP server** (`mcp_server.py`) that lets an AI agent such as Claude
  Code or Claude Desktop read the same data and drive the same approval
  workflow through tool calls.

Nothing in this project can change a real AWS account by accident: every
mutating action (stopping an instance, resizing it, changing a budget) passes
through an explicit human approval step and a set of safety guardrails
described later in this document.

## Key concepts for beginners

| Term | Meaning in this project |
| --- | --- |
| **Pipeline** | The fixed sequence of steps that turns raw cost data into a finished report. See [The 5-step analysis pipeline](#the-5-step-analysis-pipeline). |
| **LLM** | Large Language Model. This project calls one through the [OpenRouter](https://openrouter.ai/) API to normalize data, write summaries, and generate recommendations. |
| **Orchestrator** | The piece of code that runs the pipeline steps in the right order, some in sequence and some in parallel. Two implementations exist here, see below. |
| **LangGraph / LangChain** | Libraries for building multi-step, multi-agent LLM applications as a graph of steps. This project has an LLM-orchestrator built with plain Python (`src/orchestrator.py`, currently active) and an equivalent one built with LangGraph (`src/orchestrator_graph.py`, tested but not yet switched on — see its own docstring). |
| **boto3** | The official AWS SDK for Python. Used for every real AWS API call this project makes. |
| **Postgres** | The durable database this project writes every chat answer, pipeline run, and approval decision to. Runs in a Docker container by default (see [Data storage](#data-storage-postgres--redis)). |
| **Redis** | An in-memory cache used only to speed up the chat widget (skip repeat LLM calls, remember the last few turns of a conversation). Deliberately *not* durable — it is expected to empty out whenever it restarts. |
| **MCP (Model Context Protocol)** | An open protocol that lets an AI assistant (like Claude) call a program's functions as "tools" in a controlled way. `mcp_server.py` exposes this project's read and approval functions as MCP tools. |
| **RE team** | "Resource Efficiency" team — the human role in this project that reviews and approves cost-saving recommendations before they are applied. |
| **Recommendation lifecycle** | The staged approval process a recommendation goes through: raised, staged (approved or declined), then committed (applied). See [The approval workflow](#the-approval-workflow-human-in-the-loop). |

## Architecture overview

### End-to-end system

The three entry points (CLI, web server, MCP server) all call the same
orchestrator, which reads from one of three interchangeable input sources and
writes a report to `outputs/`. The web server additionally persists every
chat answer and pipeline result to Postgres, and uses Redis to speed up
repeat chat questions.

```mermaid
flowchart LR
    subgraph EntryPoints["How you can run this project"]
        CLI["main.py\n(command line)"]
        WEB["server.py\n(web dashboard + chat)"]
        MCP["mcp_server.py\n(AI agent tools)"]
    end

    subgraph Sources["Input sources (pick one or more)"]
        CSV["Local CSV files\n(docs/*.csv)"]
        CE["Cost Explorer API\n(boto3, needs credentials)"]
        CW["CloudWatch API\n(boto3, needs credentials)"]
    end

    REALAWS["Real AWS\n(EC2 / Cost Explorer / CloudWatch / Budgets)"]

    CLI --> ORCH
    WEB --> ORCH
    MCP --> ORCH

    CSV --> ORCH
    CE --> ORCH
    CW --> ORCH

    ORCH["Orchestrator\n(src/orchestrator.py)"] --> PIPE["5-step pipeline\n(see diagram below)"]
    PIPE --> OUT["outputs/insights_*.json\noutputs/insights_*.md"]

    OUT --> APPROVAL["Approval workflow\n(recommendations only)"]
    APPROVAL -->|approved & committed| REALAWS

    CE -.-> REALAWS
    CW -.-> REALAWS

    WEB --> PG[("Postgres\nevery chat answer,\npipeline run, decision")]
    WEB --> REDIS[("Redis\nchat cache + short\nconversation memory")]
```

## The 5-step analysis pipeline

This is the core of `src/orchestrator.py` and `src/pipeline/`. Steps 3.1,
3.2, and 3.3 run **in parallel** because they are independent analyses over
the same normalized data.

```mermaid
flowchart TD
    A["Step 1: Normalize\n(LLM cleans and standardizes raw data)"] --> B["Step 2: Load context\n(recent history, prior runs)"]
    B --> C1["Step 3.1: Charts\n(LLM produces chart specs)"]
    B --> C2["Step 3.2: Analysis\n(LLM finds anomalies, drivers)"]
    B --> C3["Step 3.3: Summary\n(LLM writes a plain-English summary)"]
    C1 --> D["Step 4: Combine\n(merge all three outputs)"]
    C2 --> D
    C3 --> D
    D --> E["Step 5: Finalize\n(write insights_*.json and .md)"]

    B -.also feeds.-> F1["Forecast agent\n(trend + 30-day projection)"]
    B -.also feeds.-> F2["Tag governance agent\n(untagged spend)"]
    B -.also feeds.-> F3["Root-cause agent\n(explains anomalies)"]
    F1 --> D
    F2 --> D
    F3 --> D
```

Around this core flow, several "added capabilities" run automatically, each
its own small module in `src/capabilities/`:

| Module | What it does |
| --- | --- |
| `validation.py` | Drops empty rows / rows missing required fields, and logs how many. |
| `deduplication.py` | Drops exact-duplicate rows per table. |
| `normalization.py` | Standardizes column names, casing, numeric and date formats. |
| `enrichment.py` | Adds derived fields (cost tier, date components) so later steps don't have to recompute them. |
| `metadata.py` | `MetadataTracker` — records which sources, stages, and timings a run went through, for the final report's audit trail. |
| `anomaly_detection.py` | Deterministic (non-LLM) cost-spike detection per table/dimension — feeds Step 3.2. |
| `forecasting.py` | Projects the next 30 days of spend from the recent daily trend. |
| `tag_governance.py` | Deterministic check for spend on resources missing a governance tag. |
| `root_cause.py` | Explains an anomaly by finding which dimension (service, region, tag...) actually drove it. |

Every module here is a **deterministic, non-LLM function** except the three
steps inside the diagram above (1, 3.1, 3.2, 3.3) — so a cost spike is always
found the same way twice, only its *explanation* comes from the LLM.

### The "EC2 Rightsizing" target: two independent pieces, merged

Picking **EC2 Rightsizing** in the dashboard's Target dropdown runs the same
5-step pipeline above — but pointed at its own dedicated CSV folder,
`docs/ec2_rightsizing/`, kept separate from the CSVs the other two targets
read — purely to produce **analysis and charts** for the page. It never
produces a recommendation of its own. Separately, and unconditionally, a
live AWS fleet scan runs and is the *only* source of this target's actual
recommendation (the deterministic nano⇄micro instance-size swap). The two
results are merged into one report before being shown on screen.

```mermaid
flowchart LR
    RUN["Run 'EC2 Rightsizing'"] --> A["5-step pipeline\nover docs/ec2_rightsizing/*.csv\n(analysis & charts only)"]
    RUN --> B["Live AWS fleet scan\n(boto3 describe_instances)"]
    B --> C["Deterministic nano ⇄ micro\nrecommendation"]
    A --> MERGE["One merged report"]
    C --> MERGE
    MERGE --> UI["Dashboard"]
```

If the CSV-driven analysis fails for any reason (bad data, an LLM hiccup),
the run still completes with the fleet-scan recommendation intact — the two
halves never block each other.

## Data storage: Postgres + Redis

Two different databases back the web dashboard, each doing a different job.
Both are started with one command (`docker compose up -d`, see
[Installation](#installation)) and both are optional in the sense that the
dashboard still works without them — it just stops remembering things.

```mermaid
flowchart TD
    subgraph Postgres["Postgres — durable, unbounded"]
        AR[("agent_responses\nevery chat answer +\nevery pipeline/EC2-rightsizing run,\nas JSON")]
        DL[("decision_log\nevery raise / stage /\napprove / decline / rollback")]
    end
    subgraph Redis["Redis — fast, deliberately temporary"]
        CACHE[("answer cache\nskip the LLM call for\na repeat question, 1h TTL")]
        CTX[("conversation context\nlast ~8 turns per chat session,\n6h TTL")]
    end

    CHAT["Chat widget"] --> CACHE
    CHAT --> CTX
    CHAT --> AR
    RUN["A pipeline / EC2-rightsizing run"] --> AR
    APPROVE["Raise / stage / approve /\ndecline / rollback"] --> DL
```

- **Postgres is the permanent record.** Every question asked of the chat
  widget, every pipeline run, and every approval decision is written here as
  JSON, and is never automatically deleted. It survives restarts because its
  data lives in a Docker *volume* (`docker-compose.yml`'s `arrk_pg_data`).
- **Redis is a cache, on purpose not durable.** Its container is started with
  persistence turned off (`--save "" --appendonly no`, no volume), so
  restarting the `redis` container empties it immediately — a stale cached
  answer can never outlive the process that served it. The chat widget still
  works perfectly with Redis unreachable; it just calls the LLM every time
  instead of reusing a recent answer, and forgets earlier turns in the
  conversation.
- Both are **best-effort**: if the database is down or misconfigured, the
  chat widget and the pipeline keep working — they simply stop persisting
  or caching, and a warning is logged. Nothing about answering a question or
  running the pipeline ever depends on either database being up.
- The code lives in `src/storage/postgres.py` (schema + read/write helpers)
  and `src/chat/memory.py` (the Redis cache and conversation-context
  classes).

## Directory layout

```
arrk_docs_agent_aws/
├── main.py                    # CLI entry point — run the pipeline from a terminal
├── server.py                  # Web dashboard + chat + approval API (Server-Sent Events)
├── mcp_server.py               # Launches the MCP server (stdio transport, for AI agents)
├── docker-compose.yml          # Postgres + Redis for local development
├── pyproject.toml / uv.lock    # Dependencies, managed with uv
├── .mcp.json                   # Tells Claude Code/Desktop how to start mcp_server.py
├── docs/                        # Local CSV inputs — "Local files (CSV)" target
│   ├── ec2_instance_cost.csv
│   ├── region_cost.csv
│   ├── service_daily_cost.csv
│   ├── tag_cost.csv
│   └── ec2_rightsizing/          # Separate CSV(s), read only by the "EC2 Rightsizing" target
│       └── cost_by_service_clean.csv
├── outputs/                      # Generated reports + JSON-file stores (git-ignored)
├── tests/                         # unittest suite — no LLM calls, no network calls
├── web/                            # Static HTML/CSS/vanilla JS front end (no build step)
│   ├── finops_approval_prototype.html   # served at "/" — RE team approval dashboard
│   ├── index.html                        # served at "/chat" — chat-driven pipeline UI
│   ├── apply.html                        # one-click "Apply" confirmation page (from Teams links)
│   └── app.js, shared.js, chatbot.js, *.css
└── src/
    ├── config.py                  # PipelineConfig — all settings and .env values
    ├── orchestrator.py             # Runs the 5-step flow (currently active)
    ├── orchestrator_graph.py       # LangGraph rebuild of the same flow (tested, not yet wired in)
    ├── inputs/                      # Input source connectors — same interface, different backend
    │   ├── base.py
    │   ├── local_csv.py             # ACTIVE by default
    │   ├── cost_explorer_api.py     # real boto3
    │   ├── cloudwatch_api.py        # real boto3
    │   └── factory.py
    ├── aws/
    │   ├── real_client.py           # boto3 client with a hard write-guardrail
    │   └── pricing.py               # instance-type → hourly price table
    ├── actions/                       # Human-in-the-loop: recommendation → approval → apply
    │   ├── models.py, planner.py, store.py, review.py
    │   ├── approval.py                # raise → stage → commit_batch
    │   ├── rollback.py                # undo a committed batch
    │   ├── risk.py                    # blast-radius guardrail
    │   ├── executor.py                # CSVExecutor / AWSExecutor
    │   └── decision_log.py            # audit trail — JSON file + mirrored into Postgres
    ├── storage/
    │   └── postgres.py                # agent_responses + decision_log tables
    ├── mcp_server/
    │   ├── server.py                  # MCP tool definitions
    │   ├── policy.py                   # RE-role authorization
    │   └── audit.py                     # append-only audit log (redacts secrets)
    ├── pipeline/                       # The 5 pipeline steps, one file each
    ├── capabilities/                    # Validation, dedup, normalization, enrichment, metadata,
    │                                     # anomaly detection, forecast, tag governance, root cause
    ├── chat/                            # Chat widget backend: Q&A, Redis cache, Postgres history
    ├── integrations/
    │   └── teams.py                      # Microsoft Teams webhook notifications
    ├── llm/
    │   └── client.py                      # OpenRouter API wrapper (streaming + JSON helpers)
    └── utils/
        └── logger.py
```

## Prerequisites

- **Python 3.11 or newer.**
- **[uv](https://docs.astral.sh/uv/)** — the package manager this project is
  set up for. Install it once per machine:
  ```bash
  # macOS / Linux
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
  `pip install <packages>` also works if `uv` is not available (see below),
  but `uv` is the tested, recommended path because it reads `uv.lock`.
- **An OpenRouter API key** (free tier available at
  [openrouter.ai](https://openrouter.ai/)) — required for every LLM-powered
  step. Without it, the pipeline cannot run.
- **Docker Desktop**, recommended — runs the Postgres + Redis containers the
  web dashboard uses for persistence and chat caching (see
  [Data storage](#data-storage-postgres--redis)). Not required for `main.py`,
  and `server.py` still runs without it — it just won't remember anything
  between restarts.
- **Git**, to clone the repository.
- AWS credentials are **not** required to try the "Local files (CSV)" target
  — the pipeline runs entirely against the CSVs in `docs/`. AWS credentials
  are required only for the "Real AWS" and "EC2 Rightsizing" targets, and
  even then, mutating calls stay disabled until `AWS_ALLOW_REAL_WRITES=1` is
  set (see [Real AWS access](#real-aws-access)).

## Installation

```bash
# 1. Clone the repository (skip if you already have it)
git clone https://github.com/kuldeepjeengar123/cloud-cost-optimization.git
cd cloud-cost-optimization

# 2. Install dependencies
uv sync
# or, without uv:
pip install python-dotenv requests boto3 redis "psycopg[binary]" mcp langgraph langchain-core

# 3. Create a .env file in the project root and add your LLM key
echo OPENROUTER_API_KEY=sk-or-your-key-here > .env

# 4. Start Postgres + Redis (needed for the web dashboard's chat/history/decision log)
docker compose up -d
```

Review the [environment variables reference](#environment-variables-reference)
below for every other optional setting. **Before running anything, review the
`.env` file to confirm no confidential values are committed to git** — it is
already listed in `.gitignore`.

## Running the project

There are three independent programs. Start only the ones you need.

| Command | What it does | Address |
| --- | --- | --- |
| `python main.py` | Runs the pipeline once from the terminal and writes a report to `outputs/`. | — |
| `python server.py` | Starts the web dashboard: chat UI, RE approval screen, and the HTTP/SSE API. | `http://127.0.0.1:8765` |
| `python mcp_server.py` | Starts the MCP server over stdio so an AI agent (Claude Code, Claude Desktop) can call its tools. Already configured in `.mcp.json`. | — (stdio, not a web port) |

**Always start these from the project root directory.** `.env` (and so your
LLM/AWS credentials) is only loaded automatically when the current working
directory is the repository root — starting `server.py` from anywhere else
is the most common cause of "AWS is unreachable" on the dashboard even
though your AWS credentials are correct.

### 1. Command-line report

```bash
python main.py
```

This reads the default local CSV data, runs the full pipeline, and prints
the paths to the generated `outputs/insights_<timestamp>.json` and `.md`
files, the planned recommendations (tagged `[applyable]` or `[manual]`),
whether a Teams notification was posted, the key findings, and a JSON dump
of the KPIs. If any recommendations are executable, you will be prompted
interactively to approve or skip each one.

Useful flags:

```bash
# Ask a specific question instead of the default summary
python main.py --query "Which service drove the biggest cost spike this month?"

# Point at a different folder of CSV files
python main.py --docs-folder /path/to/other/csvs

# Read from real AWS instead of local CSV, and allow it to act on the fleet
python main.py --sources cost_explorer cloudwatch --action-backend aws --apply

# Post the report card to Microsoft Teams (needs TEAMS_WEBHOOK_URL)
python main.py --notify-teams

# Auto-approve every executable recommendation without prompting
python main.py --yes
```

### 2. Web dashboard

```bash
python server.py
```

Open `http://127.0.0.1:8765` in a browser. Three pages are available:

- **`/`** — the RE team's approval dashboard (`web/finops_approval_prototype.html`): shows the queue of raised recommendations, lets a reviewer stage each one as approve/decline, and commits the whole batch at once. Its **Target** dropdown picks the analysis source:
  - **Local files (CSV)** (the default) — reads `docs/*.csv`, no AWS credentials needed.
  - **EC2 Rightsizing** — runs its own analysis pipeline over `docs/ec2_rightsizing/*.csv`, *and* separately scans your live AWS fleet for an undersized/oversized `t3.nano`/`t3.micro` instance to recommend resizing — see [the diagram above](#the-ec2-rightsizing-target-two-independent-pieces-merged). Approved changes execute against real AWS.
  - **Real AWS** — reads live Cost Explorer/CloudWatch data.
- **`/chat`** — the chat-driven pipeline UI (`web/index.html`): pick a data source (Local CSV or Real AWS), ask a question, and watch the pipeline run live.
- **`/apply`** — a one-click confirmation page opened from a Microsoft Teams notification link.

### 3. MCP server (AI agent access)

```bash
python mcp_server.py
```

See [The MCP server](#the-mcp-server-ai-agent-access) below for details.

## Environment variables reference

Set these in a `.env` file in the project root (loaded automatically via
`python-dotenv`). None of the values below should ever be committed to git
or pasted into chat, issues, or pull requests.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `OPENROUTER_API_KEY` | Yes | — | Primary LLM API key. Every LLM-powered pipeline step needs this. |
| `OPENROUTER_API_KEY1` | No | — | Optional fallback key, used automatically if the primary key fails. |
| `AWS_DEFAULT_REGION` | No | boto3 default | AWS region used for real boto3 calls. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Only for AWS-backed sources/targets | — | Standard AWS credentials. Not needed for the "Local files (CSV)" target. |
| `ACTION_BACKEND` | No | `csv` | `csv` = annotate CSV files with recommendations; `aws` = act on the real AWS API. |
| `AWS_ALLOW_REAL_WRITES` | No | `0` | Must be `1` for a real-AWS mutating call to actually execute; otherwise every write is a dry run. |
| `AWS_WRITE_ALLOWED_REGIONS` | No | *(empty = no restriction)* | Comma-separated allowlist of regions where real writes are permitted. |
| `AWS_WRITE_REQUIRE_TAG` | No | *(empty = no restriction)* | A tag key that a real-AWS resource must carry before it can be modified. |
| `AWS_WRITE_ALLOWED_INSTANCE_TYPES` | No | `t2.nano,t2.micro,t2.medium,t3.nano,t3.micro` | Comma-separated allowlist of EC2 instance types eligible for real writes (empty = no restriction). |
| `RE_TEAM_TOKEN` | No | *(empty = disabled)* | Shared secret required to stage, commit, unstage, or roll back changes. |
| `TEAMS_WEBHOOK_URL` | No | — | Microsoft Teams incoming webhook URL for run-completion notifications. |
| `PUBLIC_BASE_URL` | No | `http://127.0.0.1:8765` | Public base URL used to build clickable links inside Teams notifications and the "Apply" button. |
| `DATABASE_URL` | No | `postgresql://arrk:arrk@127.0.0.1:5432/arrk_docs_agent` | Postgres connection string — matches `docker-compose.yml`'s `db` service by default. See [Data storage](#data-storage-postgres--redis). |
| `REDIS_URL` | No | `redis://127.0.0.1:6379/0` | Redis connection string for the chat answer cache and conversation context — falls back to an in-memory (per-process) cache if unreachable. |

## Real AWS access

Every AWS-backed source and target (`cost_explorer`, `cloudwatch`, the "Real
AWS" and "EC2 Rightsizing" dashboard targets) uses `boto3` against your real
account. Set credentials in `.env`:

```bash
# In .env
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=eu-west-1
```

```bash
python main.py --sources local_csv cost_explorer cloudwatch
```

The "Local files (CSV)" target needs none of this — it never touches AWS for
reads. The "EC2 Rightsizing" target's own analysis pipeline also only reads
its local CSV, but its recommendation (found via a separate, live fleet
scan) executes against real AWS once the RE team approves and commits it
(see below), so real credentials are needed for that target too.

### Real AWS write guardrail

Real AWS *reads* (`describe_instances`, cost/usage, budgets) run automatically
whenever credentials are present. Real *mutations* (stop, resize, terminate,
tag, budget changes) additionally require `AWS_ALLOW_REAL_WRITES=1` — until
then, every write is logged and returned as a dry run, never executed:

```
[DRY-RUN] AWS_ALLOW_REAL_WRITES=0 — would call: ec2.stop_instances(InstanceIds=['i-...'])
```

Two further settings narrow the blast radius even once writes are enabled:
`AWS_WRITE_ALLOWED_REGIONS` (region allowlist) and `AWS_WRITE_REQUIRE_TAG`
(a tag key every write target must carry), plus
`AWS_WRITE_ALLOWED_INSTANCE_TYPES` (instance-type allowlist). The check
lives in `src/aws/real_client.py`'s `_guard_write()` function — every
caller (the web dashboard, the MCP server, and any future caller) goes
through the same gate.

## The approval workflow (human-in-the-loop)

An employee raises a recommendation; only the RE team can make it real, and
only once the **entire queue** has been triaged. No individual approval can
skip ahead of the batch.

```mermaid
stateDiagram-v2
    [*] --> pending: employee sees a recommendation
    pending --> pending_re: raise_recommendation()
    pending_re --> staged_approve: RE stages "approve"
    pending_re --> staged_decline: RE stages "decline"
    staged_approve --> applied: commit_batch()\n(all items staged)
    staged_decline --> declined: commit_batch()
    applied --> rolled_back: rollback_batch()
    staged_approve --> pending_re: unstage()
    staged_decline --> pending_re: unstage()
```

Key rules:

- **Staging never touches anything.** `stage_decision()`
  (`src/actions/approval.py`) only records the RE team's intent — approve or
  decline — for one request.
- **Committing is the only path to AWS or a CSV write.** `commit_batch()`
  refuses to run while any request is still `pending_re`: every open item
  must be staged one way or the other first. A per-action failure leaves
  that one action `staged_approve` for retry instead of silently marking it
  applied — every other action in the batch still commits.
- **Every committed batch can be undone.** Before running, `commit_batch()`
  snapshots each target's pre-change state to
  `outputs/applied/<batch_id>/rollback.json`. `rollback_batch()` reverses
  whatever is reversible (stop → start, resize → resize back, tag →
  restore) and reports terminated instances as irreversible rather than
  silently skipping them.
- **The web dashboard enforces the same gate a human is held to** — the RE
  view shows a live commit-readiness panel (undecided / staged-approve /
  staged-decline counts, estimated savings) and a "Commit N changes" button
  that stays disabled until the queue is empty.
- **`RE_TEAM_TOKEN`** (optional) additionally gates stage, unstage, commit,
  and rollback behind a shared secret (`X-RE-Token` header on the HTTP API,
  `re_token` argument on MCP tools) — unset by default for local or demo
  use. This is a shared secret, not real authentication; a production
  deployment needs SSO- or IAM-backed role checks in front of this server.
- **Every decision is recorded twice.** `outputs/decision_log.json` is the
  file the dashboard's "Activity log" panel reads from live; every entry is
  also mirrored into Postgres's `decision_log` table as a second, unbounded,
  durable copy — see [Data storage](#data-storage-postgres--redis).

## The chat widget

The floating "Platform Assistant" button (bottom-right of the dashboard)
answers questions grounded in whatever the dashboard is currently showing —
the latest run's insights, the live approval queue, and the currently
selected dashboard filters.

- **It knows your current filters.** Changing the Target dropdown, date
  range, or fleet environment/region/project filter updates a small strip at
  the top of the chat panel and is sent with every question, so an answer
  reflects what's on screen right now — not a stale earlier selection. If
  you've changed the Target but haven't clicked "Start run" yet, the
  assistant will say so plainly instead of pretending the dashboard has
  already refreshed.
- **"Start new chat"** (the pencil icon in the chat header) clears the
  visible conversation and starts a fresh backend session — no memory of the
  previous conversation carries over.
- **Redis caches repeat questions** (`AnswerCache`, 1-hour TTL) so asking the
  same question twice doesn't call the LLM twice — a failed/fallback answer
  is never cached, only a genuine one.
- **Redis also remembers the last ~8 turns of a conversation** so a
  follow-up question can build on what was just discussed — scoped so a new
  pipeline run or filter change never leaks an old, unrelated answer into a
  fresh one.
- **Postgres keeps a permanent transcript** of every question and answer —
  see [Data storage](#data-storage-postgres--redis).

## The MCP server (AI agent access)

`src/mcp_server/server.py` exposes this same workflow as MCP tools, so an
agent (Claude Code, Claude Desktop, or any other MCP client) can read the
fleet, costs, and recommendations, and drive the same approval flow — but it
can never reach AWS by any path a human reviewer does not also have to go
through. Reads are open to any caller; every write funnels through
`raise_recommendation` → (RE-only) `stage_recommendation_decision` on every
open request → (RE-only) `commit_approved_changes`. There is deliberately no
standalone "just call AWS" tool, not even for budgets. Every call, whether
allowed or denied, is appended to `outputs/mcp_audit.jsonl`.

Registered tools:

| Reads (open to any caller) | Writes (RE-only where noted) |
| --- | --- |
| `list_recommendations` | `raise_recommendation` |
| `get_recommendation` | `stage_recommendation_decision` (RE-only) |
| `preview_action` | `unstage_recommendation_decision` (RE-only) |
| `describe_fleet` | `commit_approved_changes` (RE-only) |
| `get_cost_and_usage` | `rollback_batch_changes` (RE-only) |
| `get_metric_statistics` | `withdraw_recommendation` |
| `list_budgets` | |
| `commit_readiness` | |
| `get_decision_log` | |
| `get_mcp_audit_log` | |

```bash
python mcp_server.py              # stdio transport
```

`.mcp.json` at the repository root points Claude Code at it automatically
(`command: .venv/Scripts/python.exe` on Windows; use `.venv/bin/python` on
macOS/Linux).

## Testing

The `tests/` folder uses Python's built-in `unittest` framework (17 test
methods across 3 files, as of this writing). No test makes an LLM call or a
network call — a fake LLM client is substituted wherever one would otherwise
be invoked, so each pipeline step's fallback behavior is exercised without
spending API quota.

| File | Covers |
| --- | --- |
| `test_capabilities.py` | The deterministic (non-LLM) capability modules: forecasting, tag governance, and root-cause correlation against known anomaly-detection findings. |
| `test_orchestrator_agents.py` | The orchestrator's parallel forecast/tag-governance/root-cause runners never raise even if the underlying function throws, and respect their capability on/off toggles. |
| `test_orchestrator_graph.py` | The LangGraph rebuild (`orchestrator_graph.py`): result shape, step event ordering, and that its three parallel agents really run concurrently, not one after another. |

```bash
# Run the full unit test suite
.venv/Scripts/python.exe -m unittest discover -s tests -v
# (pytest can also discover and run the same tests, since they subclass unittest.TestCase)
```

## Troubleshooting / FAQ

**"Missing OPENROUTER_API_KEY" or similar error on startup.**
Confirm a `.env` file exists in the project root and contains
`OPENROUTER_API_KEY=...`. `python-dotenv` only loads `.env` from the current
working directory, so run commands from the repository root.

**The dashboard says "AWS is unreachable right now" even though my
credentials work.**
This almost always means `server.py` was started from a directory other than
the project root, so `.env` (and your AWS credentials with it) was never
loaded by that process — even though AWS itself is perfectly reachable. Stop
that process and restart it with `cd` into the repository root first, then
`python server.py`. Also try the dashboard's "Refresh" button, which forces a
fresh read past any short-lived cache.

**"Address already in use" when starting `server.py`.**
Another process is already using port `8765`. Stop that process, or pass a
different port: `python server.py 8080`.

**The chat widget gives a generic "could not produce a grounded answer"
message.**
This means the LLM call itself failed or returned nothing usable (a
temporarily overloaded free model, a rate limit, or similar) — it is never
cached, so simply asking again usually works. Check the server's console
log for the specific `llm.client` warning if it keeps happening.

**Which target should I pick in the dashboard?**
The web dashboard's header has a **Target** dropdown with three options:
**Local files (CSV)** (no AWS credentials needed, the default), **EC2
Rightsizing** (its own CSV-driven analysis, plus a live-AWS-fleet-scanned
resize recommendation — needs AWS credentials to execute the approved
change), and **Real AWS** (reads live Cost Explorer/CloudWatch data). `GET
/api/target` reports which target the *last completed run* actually used —
authoritative over whatever a browser tab's `localStorage` remembers.

**Why are there two orchestrators (`orchestrator.py` and
`orchestrator_graph.py`)?**
`orchestrator.py` is a plain-Python implementation and is the one currently
used by `main.py` and `server.py`. `orchestrator_graph.py` is an equivalent
rebuild using LangGraph, built and tested to produce identical results and
events, but not yet switched on. Its module docstring documents the exact
one-line change needed to adopt it.

**A recommendation is stuck and will not commit.**
`commit_batch()` refuses to run while any item in the batch is still
`pending_re`. Every open recommendation must be staged as approve or
decline first — see [The approval workflow](#the-approval-workflow-human-in-the-loop).

**Do I need Docker / Postgres / Redis to try this project?**
No. `main.py` never touches either. `server.py` runs without them too — the
chat widget still answers questions, it just can't cache repeat questions or
remember anything after a restart, and the "Decision activity" panel only
has the local JSON file to read from rather than a permanent database copy.
Run `docker compose up -d` whenever you want that persistence back.

## Security notes

- Real AWS credentials, the OpenRouter API key, the RE team token, database
  connection strings, and any other secret must live only in the
  git-ignored `.env` file — never in code, commit messages, chat, or
  documentation.
- The default Postgres credentials in `docker-compose.yml` (`arrk`/`arrk`)
  are for local development only. Change them (and `DATABASE_URL` to match)
  before running this anywhere other than your own machine.
- Real AWS mutating actions are disabled by default (`AWS_ALLOW_REAL_WRITES=0`)
  and remain scoped by region, tag, and instance-type allowlists even once
  enabled.
- **Review every generated report, recommendation, and configuration change
  before acting on it or sharing it outside the team.** This project
  automates analysis and drafting; it does not replace human review of what
  gets applied to a real AWS account.
