"""Pipeline configuration.

Centralizes settings for input sources, LLM model selection, and capability
toggles so the same orchestrator can be driven from CSV today and the live AWS
Cost Explorer / CloudWatch APIs tomorrow with only a config change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

load_dotenv()


SourceKind = Literal["m1", "local_csv", "cost_explorer", "cloudwatch"]
DateRange = Literal["today", "yesterday", "weekly", "monthly", "yearly"]

# Rolling-window length (in days) for each preset, counting back from the
# anchor date. Shared by LocalCSVSource (anchor = latest Date in the CSVs)
# and CostExplorerSource (anchor = real now), so "monthly" means the same
# 30-day window regardless of which source produced it.
DATE_RANGE_DAYS: dict[str, int] = {
    "today": 1, "yesterday": 1, "weekly": 7, "monthly": 30, "yearly": 365,
}


@dataclass
class LLMConfig:
    api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    # Optional second key — if the primary is invalid, rate-limited, or out of
    # credit, LLMClient falls back to this one automatically. See
    # src/llm/client.py's LLMClient._request().
    api_key_fallback: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY1", ""))
    base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    model_normalize: str = "nvidia/nemotron-3-super-120b-a12b:free"
    model_charts: str = "nvidia/nemotron-3-super-120b-a12b:free"
    model_analysis: str = "nvidia/nemotron-3-super-120b-a12b:free"
    model_summary: str = "nvidia/nemotron-3-super-120b-a12b:free"
    model_chat: str = "nvidia/nemotron-3-super-120b-a12b:free"
    max_tokens: int = 4096
    stream: bool = False


@dataclass
class CapabilitiesConfig:
    validate: bool = True
    deduplicate: bool = True
    normalize: bool = True
    enrich: bool = True
    track_metadata: bool = True
    assess_risk: bool = True
    detect_anomalies: bool = True
    # Separate toggle for the heavier Isolation Forest pass within
    # detect_anomalies (see capabilities/anomaly_detection.py) - the z-score
    # pass alone is near-instant; the ML pass adds a real, one-time
    # scikit-learn/numpy import cost (several seconds, first call per
    # process) plus a smaller per-call cost after that. On by default for
    # real runs; tests that only care about something else (e.g. timing)
    # should disable this one specifically, same as assess_risk below.
    detect_anomalies_isolation_forest: bool = True
    explain_anomalies: bool = True
    forecast_costs: bool = True
    check_tag_governance: bool = True
    # Output-side guardrails (capabilities/output_guardrails.py): PII
    # redaction + basic content-policy check on the final payload, plus an
    # additive encrypted-at-rest copy alongside the existing plaintext
    # insights_*.json/.md. Toxicity detection and data-leakage prevention
    # are explicitly out of scope (integration Step 8 decision), not
    # partially implemented here.
    output_guardrails: bool = True
    # The real M1 -> M2 hop (integration Step 12): M1Source runs each line
    # item through M2's normalise+enrich graph before M3 ever sees it. OFF
    # BY DEFAULT, unlike every other toggle above - this is the one capability
    # that makes real external calls (LLM completion + embeddings), not pure
    # local computation. Flip to True only once agent_module/.env has a real
    # OPENROUTER_API_KEY and you're ready to spend real (if small, given the
    # demo dataset's size) LLM/embedding calls.
    m2_enrichment: bool = False


@dataclass
class PipelineConfig:
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    # Integration Step 11: m1 (ingestion/'s real, validated/masked/
    # encrypted output) is the real production default. local_csv (docs/*.csv)
    # was a developer's own test fixture, not real M1 output - kept available
    # for dev/testing (pass sources=["local_csv"] explicitly), just no longer
    # what a plain PipelineConfig() gives you.
    sources: list[SourceKind] = field(default_factory=lambda: ["m1"])
    docs_folder: Path = field(default=Path("docs"))
    # Sibling folder at the repo root (see ingestion/), not nested under src/ -
    # M1 is a separate, independently runnable/containerized module. Renamed
    # from m1_ingestion/ during the folder reorg (integration Step 13).
    m1_folder: Path = field(default=Path("ingestion"))
    output_folder: Path = field(default=Path("outputs"))
    # Where applied (human-approved) changes are written. The CSV backend writes
    # modified copies here under <run_id>/ so the source docs/ stay untouched.
    applied_folder: Path = field(default=Path("outputs/applied"))
    llm: LLMConfig = field(default_factory=LLMConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
    user_query: str = "Provide a comprehensive AWS cost analysis with key insights and recommendations."
    # Scopes input data to a rolling window before it reaches the pipeline.
    # None (or any value outside DATE_RANGE_DAYS) means "all available data".
    date_range: Optional[str] = None

    # --- Teams + actionable recommendations ---
    # Where the report card is pushed (Teams Incoming Webhook URL). Empty = no push.
    teams_webhook_url: str = field(default_factory=lambda: os.getenv("TEAMS_WEBHOOK_URL", ""))
    # Base URL the "Apply" buttons link back to. Defaults to the local web server.
    # Swap for a tunnel/host URL when the loop must work from outside this machine.
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8765"))
    # Which executor runs when a recommendation is applied:
    #   "csv" -> annotate the matching row in docs/*.csv (Phase 1, default)
    #   "aws" -> call the real AWS API (gated by aws_allow_real_writes below)
    action_backend: Literal["csv", "aws"] = field(default_factory=lambda: os.getenv("ACTION_BACKEND", "csv"))

    # --- Real AWS write gate ---
    # Real AWS *reads* (describe_instances, cost/usage, budgets) always run
    # once boto3 credentials are present. Real *mutations*
    # (stop/resize/terminate/tag/budget) additionally require this flag, so
    # wiring up the real client is safe to ship well before anyone flips it —
    # every write instead logs the exact boto3 call it would have made and
    # returns without executing. Same guardrail applies whether the call comes
    # through commit_batch(), the MCP server, or any other caller of
    # AWSExecutor — it lives in the client, not any one entry point.
    aws_allow_real_writes: bool = field(default_factory=lambda: os.getenv("AWS_ALLOW_REAL_WRITES", "0") == "1")
    # Extra resource allowlist so even a live, write-enabled run can't touch an
    # instance outside these regions (empty = no region restriction) or one
    # missing this tag key (empty = no tag restriction, e.g. "CostOptimizable").
    aws_write_allowed_regions: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            r.strip() for r in os.getenv("AWS_WRITE_ALLOWED_REGIONS", "").split(",") if r.strip()
        )
    )
    aws_write_require_tag: str = field(default_factory=lambda: os.getenv("AWS_WRITE_REQUIRE_TAG", ""))
    # Cap on resize's *target* instance type — the only thing a real resize is
    # allowed to land on (empty = no restriction). Defaults to the smallest t2
    # sizes, per an explicit ask to keep any resize (agent-raised or
    # human-approved) from ever landing on anything larger than t2.medium, plus
    # t3.nano/t3.micro for the EC2 rightsizing use case.
    aws_write_allowed_instance_types: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            t.strip() for t in os.getenv(
                "AWS_WRITE_ALLOWED_INSTANCE_TYPES", "t2.nano,t2.micro,t2.medium,t3.nano,t3.micro"
            ).split(",") if t.strip()
        )
    )

    # --- RE-team gate (Phase 5) ---
    # Shared-secret header (X-RE-Token) required on stage/unstage/commit/
    # rollback endpoints once set. Empty (default) leaves those endpoints open
    # for local/demo use — see server.py's _require_re_role().
    re_team_token: str = field(default_factory=lambda: os.getenv("RE_TEAM_TOKEN", ""))

    # --- Chat widget (Redis cache + local SQLite question log) ---
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    chat_db_path: Path = field(default=Path("outputs/chat_history.sqlite3"))

    def __post_init__(self) -> None:
        if not self.docs_folder.is_absolute():
            self.docs_folder = self.project_root / self.docs_folder
        if not self.m1_folder.is_absolute():
            self.m1_folder = self.project_root / self.m1_folder
        if not self.output_folder.is_absolute():
            self.output_folder = self.project_root / self.output_folder
        if not self.applied_folder.is_absolute():
            self.applied_folder = self.project_root / self.applied_folder
        if not self.chat_db_path.is_absolute():
            self.chat_db_path = self.project_root / self.chat_db_path
        self.output_folder.mkdir(parents=True, exist_ok=True)


def load_config(**overrides) -> PipelineConfig:
    cfg = PipelineConfig()
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
