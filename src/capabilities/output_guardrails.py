"""Output-side guardrails - M4 Step 5's security layer, distinct from M1's
input-side guardrails (ingestion/pii.py, encryption.py). Per the
architecture diagram's "Output Guardrails & Security" box: PII masking &
redaction, policy enforcement, and encryption at rest. Toxicity detection
and data-leakage prevention are explicitly deferred (integration Step 8
scope decision) - not attempted here, not partially stubbed.

The PII and encryption logic mirror ingestion/pii.py and
ingestion/encryption.py's approach exactly (same regex patterns, same
Fernet scheme) but are re-implemented here rather than imported - this repo
already has a deliberate precedent for that (see agents/agent_module/
m1_bridge.py's own docstring): M1, M2, and the src/ pipeline are
independently runnable modules, and cross-importing between them would
break that.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

# --- PII redaction - same patterns/order as ingestion/pii.py -----------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ARN_RE = re.compile(r"arn:aws:[a-zA-Z0-9\-]*:[a-zA-Z0-9\-]*:\d{12}:[\w\-/:.*]+|arn:aws:[a-zA-Z0-9\-]*:::[\w\-/.*]+")
ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")

# Order matters: ARNs contain account IDs, so mask ARNs first or the
# account-id pattern would eat the digits inside the ARN before ARN_RE gets
# to match it (same reasoning as ingestion/pii.py).
_PII_PATTERNS = [
    ("ARN", ARN_RE),
    ("EMAIL", EMAIL_RE),
    ("ACCOUNT_ID", ACCOUNT_ID_RE),
]


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Returns (redacted_text, list of PII types found)."""
    found: list[str] = []
    for label, pattern in _PII_PATTERNS:
        if pattern.search(text):
            found.append(label)
            text = pattern.sub(f"[REDACTED_{label}]", text)
    return text, found


# --- Content policy - basic denylist keyword matching ----------------------
# Deliberately simple, deterministic substring matching - NOT a toxicity
# classifier (explicitly deferred). Flags matches for review; does not
# attempt to guess intent or context.

DEFAULT_POLICY_DENYLIST = [
    # Example forbidden phrases for a customer-facing cost report - replace
    # with whatever your organization's actual policy list is. Left small
    # and clearly a placeholder rather than guessing at real requirements.
    "internal use only",
    "do not distribute",
]


def check_content_policy(text: str, denylist: list[str] | None = None) -> list[str]:
    """Returns the list of denylisted phrases found in `text` (case-
    insensitive substring match), or [] if none. Never raises, never
    modifies the text - the caller decides what to do with a hit (redact,
    flag for review, block delivery, etc.)."""
    denylist = DEFAULT_POLICY_DENYLIST if denylist is None else denylist
    lowered = text.lower()
    return [phrase for phrase in denylist if phrase.lower() in lowered]


# --- Recursive payload redaction -------------------------------------------

def redact_payload(payload: Any, denylist: list[str] | None = None) -> tuple[Any, dict]:
    """Walk a JSON-shaped payload (dict/list/str/anything), applying
    redact_pii and check_content_policy to every string value found,
    anywhere in the structure - not just a fixed set of known fields, since
    the "combined" payload's shape varies (KPIs, findings, recommendations,
    chart specs, ...).

    Returns (redacted_payload, report) where report is:
        {"pii_hits": {"<json-path>": ["EMAIL", ...], ...},
         "policy_hits": {"<json-path>": ["internal use only", ...], ...}}
    Empty dicts in report mean nothing was found - never an error condition.
    """
    report: dict[str, dict] = {"pii_hits": {}, "policy_hits": {}}

    def _walk(node: Any, path: str) -> Any:
        if isinstance(node, str):
            redacted, pii_found = redact_pii(node)
            if pii_found:
                report["pii_hits"][path] = pii_found
            policy_found = check_content_policy(redacted, denylist)
            if policy_found:
                report["policy_hits"][path] = policy_found
            return redacted
        if isinstance(node, dict):
            return {k: _walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
        return node

    redacted_payload = _walk(payload, "")
    return redacted_payload, report


# --- Encryption at rest - same Fernet scheme as ingestion/encryption.py -

_FOLDER = Path(__file__).parent
_KEY_PATH = _FOLDER / "secret.key"


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("OUTPUT_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    _KEY_PATH.write_bytes(key)
    return key


def get_fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_payload(payload: Any, dest_path: Path) -> None:
    """Serialize payload to JSON and write the Fernet-encrypted bytes to
    dest_path - additive alongside the existing plaintext insights_*.json/.md
    (those stay human-readable on purpose), not a replacement for them."""
    data = json.dumps(payload, default=str).encode("utf-8")
    token = get_fernet().encrypt(data)
    dest_path.write_bytes(token)


def decrypt_payload(src_path: Path) -> Any:
    """Inverse of encrypt_payload."""
    token = src_path.read_bytes()
    data = get_fernet().decrypt(token)
    return json.loads(data.decode("utf-8"))
