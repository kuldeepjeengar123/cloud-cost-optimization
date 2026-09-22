"""Outbound integrations (Microsoft Teams, ...)."""

from .teams import build_card, notify_teams, post_to_teams

__all__ = ["build_card", "notify_teams", "post_to_teams"]
