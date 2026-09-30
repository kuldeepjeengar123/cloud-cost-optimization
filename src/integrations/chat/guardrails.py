"""Secret/credential redaction guardrails for the chat widget.

Two directions of leakage this guards against:

1. **Outbound** — the LLM's answer must never contain this deployment's own
   secrets (OpenRouter API keys, the RE-team shared token, the Postgres/Redis
   connection strings, AWS credentials, the Teams webhook URL). This does not
   rely on "the prompt never includes secrets so this is unreachable" — it
   re-checks the actual text the model produced, the same belt-and-braces
   approach ``src/mcp_server/audit.py`` takes for the MCP audit log.
2. **Inbound** — a user may paste a real credential (their own AWS key, a
   database URL, etc.) into a chat question. That must never be forwarded to
   the third-party LLM API verbatim, nor persisted to Postgres history, nor
   echoed back in an answer.

Both directions share the same two-layer strategy: exact-match redaction of
this deployment's own known secret values (outbound only — we can't know a
user's own third-party secret in advance), plus generic regex patterns for
common credential shapes (AWS access key IDs, OpenRouter/OpenAI-style keys,
connection strings with embedded credentials, bearer tokens, labeled
key=value secrets) so an unconfigured or third-party secret is still caught
in both directions.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...config import PipelineConfig

REDACTED = "[REDACTED]"

# Deliberately narrow and distinctive shapes only — a generic "any long
# base64-looking string" pattern would also catch resource IDs, hashes, and
# other legitimate cost-data text, mangling answers instead of protecting
# anything.
_GENERIC_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"sk-(?:or-)?[A-Za-z0-9_-]{16,}"),  # OpenRouter / OpenAI-style API keys
    re.compile(r"(?i)\b(?:postgres(?:ql)?|redis)://[^\s\"'<>]*:[^\s\"'<>]*@[^\s\"'<>]+"),  # connection strings w/ creds
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.]{10,}"),  # bearer tokens
    re.compile(r"(?i)(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+]{8,})['\"]?"),
]


def _known_secrets(cfg: "PipelineConfig") -> list[str]:
    """Every literal secret value this deployment actually holds right now,
    longest first so a substring (e.g. a key embedded inside a longer
    connection string) doesn't leave a shorter fragment unredacted."""
    values = [
        cfg.llm.api_key,
        cfg.llm.api_key_fallback,
        cfg.re_team_token,
        cfg.database_url,
        cfg.redis_url,
        cfg.teams_webhook_url,
        os.getenv("AWS_ACCESS_KEY_ID", ""),
        os.getenv("AWS_SECRET_ACCESS_KEY", ""),
    ]
    # Skip anything short or unset — redacting a 1-2 character default string
    # would mangle unrelated text instead of protecting anything.
    return sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def redact_secrets(text: str, cfg: "PipelineConfig") -> str:
    """Belt-and-braces scrub applied to every LLM answer (streamed or not)
    before it is returned to the browser, cached, or written to Postgres
    history. A no-op on text that has no secrets in it."""
    if not text:
        return text
    for secret in _known_secrets(cfg):
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _GENERIC_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def scrub_user_input(text: str) -> str:
    """Applied to the raw question before it is sent to the LLM or persisted.
    A user pasting their own credential into the chat box must not have it
    forwarded to a third-party API or stored in our database — only generic
    shape-based detection applies here, since a third-party secret's literal
    value can't be known in advance."""
    if not text:
        return text
    for pattern in _GENERIC_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
