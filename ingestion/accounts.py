"""AWS account allow/deny gate for ingested cost line items.

DENYLIST always wins. If ALLOWLIST is non-empty, any account not in it is
also rejected (allowlist mode); if ALLOWLIST is empty, every account is
accepted unless it's denylisted.

Override via env vars for real deployments, e.g.:
    ACCOUNT_DENYLIST=999999999999,888888888888
    ACCOUNT_ALLOWLIST=123456789012
"""
import os

_DEFAULT_DENYLIST = {"999999999999"}
_DEFAULT_ALLOWLIST = set()  # empty = allow anything not denylisted


def _load_set(env_var: str, default: set) -> set:
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    return {v.strip() for v in raw.split(",") if v.strip()}


DENYLIST = _load_set("ACCOUNT_DENYLIST", _DEFAULT_DENYLIST)
ALLOWLIST = _load_set("ACCOUNT_ALLOWLIST", _DEFAULT_ALLOWLIST)


def check_account(account_id: str) -> str | None:
    """Returns a rejection reason string, or None if the account is permitted."""
    if account_id in DENYLIST:
        return f"account {account_id} is denylisted"
    if ALLOWLIST and account_id not in ALLOWLIST:
        return f"account {account_id} is not in the allowlist"
    return None
