"""
Thalamus Notifier — sends alerts via Discord webhook when the analyst
identifies something actionable.
"""

import json
import os
import httpx
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
ALERTS_DIR = ROOT / "memory" / "alerts"

MAX_FIELD = 1024
MAX_DESC = 4096


def _trunc(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def format_alert_discord(analysis: dict) -> list[dict]:
    """Format an analysis result into Discord embed(s)."""
    embeds = []

    # Main alert embed with events
    events = analysis.get("events_analyzed", [])
    events_text = "\n".join(f"- {e}" for e in events[:8])

    main_embed = {
        "title": "THALAMUS ALERT",
        "description": _trunc(events_text, MAX_DESC) if events_text else "New geopolitical analysis",
        "color": 0xE74C3C,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    embeds.append(main_embed)

    # One embed per trade idea — thesis goes in description (4096 limit)
    for idea in analysis.get("trade_ideas", []):
        direction = idea.get("direction", "").upper()
        instrument = idea.get("instrument", "")
        confidence = idea.get("confidence", "unknown")
        horizon = idea.get("time_horizon", "unknown")
        order = idea.get("order", "")

        chain = idea.get("chain", [])
        chain_text = "\n".join(f"{i+1}. {step}" for i, step in enumerate(chain))

        thesis = idea.get("thesis", "")
        counter = idea.get("counter_thesis", "")

        color = {
            "high": 0x2ECC71,
            "medium": 0xF39C12,
            "low": 0x95A5A6,
        }.get(confidence.split("-")[0] if "-" in confidence else confidence, 0x95A5A6)

        # Put thesis in description (bigger limit) instead of a field
        trade_embed = {
            "title": f"{direction} {instrument}",
            "description": _trunc(thesis, MAX_DESC),
            "color": color,
            "fields": [
                {"name": "Confidence", "value": confidence, "inline": True},
                {"name": "Horizon", "value": horizon, "inline": True},
            ],
        }

        if order:
            trade_embed["fields"].append(
                {"name": "Order", "value": order, "inline": True}
            )

        if chain_text:
            trade_embed["fields"].append(
                {"name": "Chain of Reasoning", "value": _trunc(chain_text, MAX_FIELD), "inline": False}
            )

        if counter:
            trade_embed["fields"].append(
                {"name": "Counter-thesis", "value": _trunc(counter, MAX_FIELD), "inline": False}
            )

        embeds.append(trade_embed)

    return embeds


def save_alert(analysis: dict):
    """Save alert to disk for history tracking."""
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filepath = ALERTS_DIR / f"{timestamp}.json"
    filepath.write_text(json.dumps(analysis, indent=2))
    return filepath


def send_discord_alert(analysis: dict, webhook_url: str):
    """Send alert via Discord webhook. Sends one message per embed
    to stay under Discord's 6000 char total embed limit."""
    embeds = format_alert_discord(analysis)

    for embed in embeds:
        payload = {
            "username": "Thalamus",
            "embeds": [embed],
        }
        resp = httpx.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()
