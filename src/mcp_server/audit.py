"""Append-only audit log for every MCP tool call.

Every call — read or write, allowed or denied — is appended to
``outputs/mcp_audit.jsonl`` so an agent's actions show up next to human raises/
stages/commits/declines in the RE dashboard's activity log instead of being an
unobserved side channel. One JSON object per line (append-only; never
rewritten), newest entries at the end of the file.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_REDACT_KEYS = ("re_token", "token", "api_key", "password", "secret")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(arguments: dict) -> dict:
    return {k: ("***" if k in _REDACT_KEYS and v else v) for k, v in arguments.items()}


def _safe(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except TypeError:
        return str(value)


def log_call(
    path: Path,
    tool: str,
    arguments: dict[str, Any],
    role: str,
    allowed: bool,
    result: Any = None,
    error: str | None = None,
) -> None:
    entry = {
        "at": _now(),
        "tool": tool,
        "arguments": _redact(arguments),
        "role": role,
        "allowed": allowed,
        "result": _safe(result),
        "error": error,
    }
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def read_all(path: Path, limit: int = 200) -> list[dict]:
    """Most recent ``limit`` entries, newest first."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))
