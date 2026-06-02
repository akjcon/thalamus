"""
Thalamus Replay — rerun analysis on saved cycle data.
Use this to iterate on analyst prompts without re-fetching
headlines or re-classifying. Only pays for the analysis step.

Usage:
    python3 replay.py                    # replay the most recent cycle
    python3 replay.py 20260302_232111    # replay a specific cycle
    python3 replay.py --list             # list available replays
"""

import sys
import json
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(Path(__file__).parent.parent / ".env")

from analyst import load_world_model, load_portfolio, deep_analysis, validate_prices, update_world_model

ROOT = Path(__file__).parent.parent
REPLAY_DIR = ROOT / "memory" / "replays"


def list_replays():
    if not REPLAY_DIR.exists():
        print("No replays saved yet. Run a scan cycle first.")
        return

    replays = sorted(REPLAY_DIR.glob("*.json"), reverse=True)
    if not replays:
        print("No replays saved yet. Run a scan cycle first.")
        return

    print("Available replays:\n")
    for r in replays:
        data = json.loads(r.read_text())
        n_flagged = len(data.get("flagged", []))
        ts = data.get("timestamp", r.stem)
        titles = [f.get("title", "?")[:60] for f in data.get("flagged", [])[:3]]
        print(f"  {r.stem}  ({n_flagged} flagged items)")
        for t in titles:
            print(f"    - {t}")
        print()


def replay(replay_id: str | None = None, apply_updates: bool = False):
    if not REPLAY_DIR.exists():
        print("No replays saved yet. Run a scan cycle first.")
        return

    if replay_id:
        replay_file = REPLAY_DIR / f"{replay_id}.json"
    else:
        # Most recent
        replays = sorted(REPLAY_DIR.glob("*.json"), reverse=True)
        if not replays:
            print("No replays saved yet.")
            return
        replay_file = replays[0]

    if not replay_file.exists():
        print(f"Replay not found: {replay_file}")
        return

    data = json.loads(replay_file.read_text())
    flagged = data["flagged"]
    research = data.get("research", "")

    print(f"Replaying: {replay_file.stem}")
    print(f"  {len(flagged)} flagged items:")
    for item in flagged:
        print(f"  [{item.get('urgency', '?').upper()}] {item.get('title', '?')}")

    print(f"\nLoading world model and portfolio...")
    world_model = load_world_model()
    portfolio = load_portfolio()

    print(f"Running deep analysis...\n")
    client = Anthropic()
    result = deep_analysis(client, flagged, world_model, portfolio, research, "claude-sonnet-4-6")

    if "error" in result:
        print(f"Analysis error: {result['error']}")
        return

    # Apply world-model updates (opt-in) — replay is read-only by default, so this
    # is the explicit way to actually persist a cycle's reconciliation to disk.
    if apply_updates:
        upd = result.get("world_model_updates", "")
        if upd:
            print("\nApplying world model updates (--apply)...")
            update_world_model(client, world_model, upd, "claude-sonnet-4-6")
            print("World model updates applied.")
        else:
            print("\n(--apply) No world_model_updates produced this run.")

    # Price validation (uses Schwab API, falls back to web_search)
    ideas = result.get("trade_ideas", [])
    if ideas and result.get("alert_worthy", False):
        print(f"\nValidating prices for {len(ideas)} trade idea(s)...")
        result["trade_ideas"] = validate_prices(client, ideas, "claude-sonnet-4-6")

    # Print results
    alert_worthy = result.get("alert_worthy", False)
    print(f"Alert worthy: {'YES' if alert_worthy else 'no'}")

    situation = result.get("situation_summary", "")
    if situation:
        print(f"\nSituation: {situation}")

    ideas = result.get("trade_ideas", [])
    if ideas:
        print(f"\n{'='*60}")
        print(f"TRADE IDEAS ({len(ideas)})")
        print(f"{'='*60}")
        for i, idea in enumerate(ideas):
            direction = idea.get("direction", "").upper()
            instrument = idea.get("instrument", "")
            one_liner = idea.get("one_liner", "")
            confidence = idea.get("confidence", "?")
            horizon = idea.get("time_horizon", "?")

            print(f"\n{i+1}. {direction} {instrument}")
            if one_liner:
                print(f"   {one_liner}")
            print(f"   Confidence: {confidence} | Horizon: {horizon}")

            chain = idea.get("chain", [])
            if chain:
                print(f"\n   How it plays out:")
                for j, step in enumerate(chain):
                    print(f"   {j+1}. {step}")

            counter = idea.get("counter_thesis", "")
            if counter:
                print(f"\n   What could go wrong:")
                print(f"   {counter}")

            overlap = idea.get("overlap_check", "")
            if overlap:
                print(f"\n   Overlap check: {overlap}")

            price_ctx = idea.get("price_context", "")
            if price_ctx:
                print(f"\n   Price context: {price_ctx}")
    else:
        print("\nNo trade ideas this cycle.")

    # Save result for comparison
    out_file = ROOT / "memory" / "replays" / f"{replay_file.stem}_result.json"
    out_file.write_text(json.dumps(result, indent=2))
    print(f"\nFull result saved to: {out_file.name}")


if __name__ == "__main__":
    args = sys.argv[1:]
    apply_updates = "--apply" in args
    args = [a for a in args if a != "--apply"]
    if args and args[0] == "--list":
        list_replays()
    elif args:
        replay(args[0], apply_updates=apply_updates)
    else:
        replay(apply_updates=apply_updates)
