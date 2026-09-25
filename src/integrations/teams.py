"""Microsoft Teams delivery via an Incoming Webhook.

Builds an Adaptive Card summarising the report and renders each recommendation
with an **Apply** / **Dismiss** button. The buttons are ``Action.OpenUrl`` links
back to ``{public_base_url}/apply?id=<action_id>`` — i.e. the link-back model:
Teams can't call localhost, so the button opens the web confirmation page where
the change is actually executed.

Payload uses the current Adaptive-Card-in-attachments shape accepted by Teams
Incoming Webhooks / Power Automate "post to channel" flows. (The legacy O365
MessageCard format is retired.)
"""

from __future__ import annotations

import json
from typing import Optional

import requests

from ..config import PipelineConfig
from ..utils.logger import get_logger
from ..actions.models import Action

log = get_logger("integrations.teams")

MAX_ACTIONS_ON_CARD = 10
_IMPACT_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟢"}


def _apply_url(base_url: str, action_id: str, decision: str = "apply") -> str:
    return f"{base_url.rstrip('/')}/apply?id={action_id}&decision={decision}"


def _facts_block(combined: dict) -> dict:
    bm = combined.get("business_metadata", {}) or {}
    kpis = (combined.get("analysis", {}) or {}).get("kpis", {}) or {}
    facts = []
    total = bm.get("total_cost_observed")
    if total is not None:
        facts.append({"title": "Total observed cost", "value": f"${float(total):.4f}"})
    for key in ("top_service", "top_region"):
        val = kpis.get(key)
        if isinstance(val, dict):
            val = ", ".join(f"{k}: {v}" for k, v in val.items())
        if val:
            facts.append({"title": key.replace("_", " ").title(), "value": str(val)})
    return {"type": "FactSet", "facts": facts or [{"title": "Status", "value": "Report ready"}]}


def _action_item(action: Action, base_url: str) -> list[dict]:
    emoji = _IMPACT_EMOJI.get(action.impact, "•")
    target = (
        f"→ `{action.table}` where {action.match_column} = **{action.match_value}**"
        if action.executable
        else "_(no matching data row — acknowledge only)_"
    )
    buttons = [
        {
            "type": "Action.OpenUrl",
            "title": "✅ Apply" if action.executable else "✔ Acknowledge",
            "url": _apply_url(base_url, action.id, "apply"),
        },
        {
            "type": "Action.OpenUrl",
            "title": "✖ Dismiss",
            "url": _apply_url(base_url, action.id, "dismiss"),
        },
    ]
    return [
        {
            "type": "TextBlock",
            "text": f"{emoji} **{action.title}**",
            "wrap": True,
            "spacing": "Medium",
        },
        {"type": "TextBlock", "text": target, "wrap": True, "isSubtle": True, "spacing": "None"},
        {"type": "ActionSet", "actions": buttons},
    ]


def build_card(cfg: PipelineConfig, combined: dict, actions: list[Action]) -> dict:
    """Return the full Teams webhook payload (message + adaptive card)."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "AWS Cost & Ops Insights",
            "weight": "Bolder",
            "size": "Large",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": cfg.user_query,
            "wrap": True,
            "isSubtle": True,
            "spacing": "None",
        },
        _facts_block(combined),
    ]

    findings = (combined.get("summary", {}) or {}).get("key_findings", []) or []
    if findings:
        body.append({"type": "TextBlock", "text": "**Key findings**", "wrap": True, "spacing": "Medium"})
        for f in findings[:5]:
            body.append({"type": "TextBlock", "text": f"• {f}", "wrap": True, "spacing": "None"})

    pending = [a for a in actions if a.status == "pending"]
    if pending:
        body.append(
            {"type": "TextBlock", "text": "**Recommended actions**", "wrap": True, "spacing": "Medium"}
        )
        for action in pending[:MAX_ACTIONS_ON_CARD]:
            body.extend(_action_item(action, cfg.public_base_url))
        extra = len(pending) - MAX_ACTIONS_ON_CARD
        if extra > 0:
            body.append(
                {"type": "TextBlock", "text": f"_+{extra} more in the full report._", "wrap": True}
            )

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


def post_to_teams(webhook_url: str, payload: dict, timeout: int = 15) -> bool:
    resp = requests.post(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    # Incoming webhooks return 200 with body "1"; Workflows return 202.
    if resp.status_code in (200, 202):
        log.info("Posted report card to Teams (HTTP %s)", resp.status_code)
        return True
    log.warning("Teams webhook returned HTTP %s: %s", resp.status_code, resp.text[:300])
    return False


def notify_teams(
    cfg: PipelineConfig, combined: dict, actions: list[Action]
) -> Optional[bool]:
    """Build and post the card. Returns None if no webhook is configured."""
    if not cfg.teams_webhook_url:
        log.info("No TEAMS_WEBHOOK_URL set; skipping Teams notification.")
        return None
    payload = build_card(cfg, combined, actions)
    try:
        return post_to_teams(cfg.teams_webhook_url, payload)
    except requests.RequestException as exc:
        log.warning("Teams notification failed: %s", exc)
        return False
