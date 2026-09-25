"""Single authorization choke point for the MCP server's RE-only tools.

Mirrors ``server.py``'s ``_require_re_role()`` HTTP header check exactly,
using the same shared-secret config field (``cfg.re_team_token``) so there is
exactly one definition of "who counts as the RE team" across the web
dashboard and MCP — not two gates that could quietly drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import PipelineConfig


class NotAuthorized(PermissionError):
    """Raised when a tool call fails the RE-team gate."""


def require_re_role(cfg: "PipelineConfig", re_token: str | None) -> None:
    """Raise :class:`NotAuthorized` unless ``re_token`` matches the
    configured ``RE_TEAM_TOKEN``. An unset token (the default, local/demo
    mode) leaves the gate open, same as the HTTP server's behaviour — this is
    a shared-secret check, not real authentication; see ``config.py``'s
    ``re_team_token`` docstring.
    """
    required = cfg.re_team_token
    if not required:
        return
    if re_token != required:
        raise NotAuthorized(
            "RE team credentials required for this action. Pass the correct "
            "re_token argument (see RE_TEAM_TOKEN in .env)."
        )
