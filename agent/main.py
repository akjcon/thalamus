"""
Thalamus Main Loop — orchestrates the scan → classify → analyze → alert cycle.
"""

import os
import time
import schedule
import yaml
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(Path(__file__).parent.parent / ".env")

from scanner import pull_feeds, classify_headlines, format_headlines_for_llm
from analyst import (
    load_world_model, load_portfolio, research_questions,
    deep_analysis, update_world_model,
)
from notifier import save_alert, send_discord_alert

ROOT = Path(__file__).parent.parent


def load_config():
    with open(ROOT / "config" / "sources.yaml") as f:
        return yaml.safe_load(f)


def run_cycle():
    """Execute one full scan-analyze cycle."""
    print(f"\n{'='*60}")
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scan cycle")
    print(f"{'='*60}")

    config = load_config()
    client = Anthropic()  # Uses ANTHROPIC_API_KEY env var
    scanner_model = config["models"]["scanner"]
    analyst_model = config["models"]["analyst"]

    # Step 1: Pull new headlines
    print("\n[1/5] Pulling RSS feeds...")
    headlines = pull_feeds(config)
    print(f"  Found {len(headlines)} new headlines")

    if not headlines:
        print("  Nothing new. Skipping cycle.")
        return

    # Step 2: Classify with Haiku
    print("\n[2/5] Classifying headlines with Haiku...")
    world_model = load_world_model()

    # Give Haiku a compressed summary, not the full model
    model_summary = world_model[:3000] if len(world_model) > 3000 else world_model
    flagged = classify_headlines(client, headlines, model_summary, scanner_model)
    print(f"  Flagged {len(flagged)} significant items")

    if not flagged:
        # Still update world model with a light touch on quiet cycles
        print("\n  Quiet cycle — no significant events detected.")
        return

    for item in flagged:
        print(f"  [{item['urgency'].upper()}] {item['title']}")
        print(f"         {item['reason']}")

    # Prioritize HIGH urgency items for deep analysis, cap total to avoid token overflow
    high = [f for f in flagged if f["urgency"] == "high"]
    medium = [f for f in flagged if f["urgency"] == "medium"]
    low = [f for f in flagged if f["urgency"] == "low"]
    analysis_items = (high + medium + low)[:10]  # Cap at 10 most important
    print(f"\n  Sending {len(analysis_items)} items to deep analysis (of {len(flagged)} flagged)")

    # Step 3: Research follow-up questions
    print("\n[3/5] Researching follow-up questions...")
    all_questions = []
    for item in analysis_items:
        all_questions.extend(item.get("follow_up_questions", []))

    research = ""
    if all_questions:
        print(f"  Researching {len(all_questions)} questions...")
        research = research_questions(client, all_questions[:5], analyst_model)  # Cap at 5

    # Step 4: Deep analysis
    print("\n[4/5] Running deep analysis with Sonnet...")
    portfolio = load_portfolio()
    result = deep_analysis(client, analysis_items, world_model, portfolio, research, analyst_model)

    if "error" in result:
        print(f"  [!] Analysis error: {result['error']}")
        return

    # Step 5: Update world model and alert if needed
    print("\n[5/5] Updating world model...")
    model_updates = result.get("world_model_updates", "")
    if model_updates:
        update_world_model(client, world_model, model_updates, analyst_model)

    # Save alert
    alert_path = save_alert(result)
    print(f"  Saved alert to {alert_path}")

    # Send notification if alert-worthy
    if result.get("alert_worthy", False):
        print("\n  *** ALERT WORTHY — sending notification ***")
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

        if webhook_url:
            try:
                send_discord_alert(result, webhook_url)
                print("  Discord alert sent.")
            except Exception as e:
                print(f"  [!] Failed to send Discord alert: {e}")
        else:
            print("  [!] Discord not configured — set DISCORD_WEBHOOK_URL env var.")

        # Print trade ideas to console regardless
        for idea in result.get("trade_ideas", []):
            print(f"\n  Trade idea: {idea.get('direction', '').upper()} {idea.get('instrument', '')}")
            print(f"  Confidence: {idea.get('confidence', '?')}")
            print(f"  Thesis: {idea.get('thesis', '?')}")
    else:
        print("\n  Not alert-worthy this cycle.")

    print(f"\n{'='*60}")
    print(f"Cycle complete.")
    print(f"{'='*60}")


def main():
    config = load_config()
    interval = config.get("scan_interval_hours", 2)

    print(f"Thalamus starting — scanning every {interval} hours")
    print(f"Press Ctrl+C to stop\n")

    # Run immediately on start
    run_cycle()

    # Then schedule recurring runs
    schedule.every(interval).hours.do(run_cycle)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
