"""
Thalamus Discord Bot — the main process.
Runs scan cycles on a timer and provides interactive alerts
with expandable trade ideas and chat capability.
"""

import os
import json
import asyncio
import discord
from discord import ui
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(Path(__file__).parent.parent / ".env")

from scanner import pull_feeds, classify_headlines
from analyst import (
    load_world_model, load_portfolio, research_questions,
    deep_analysis, update_world_model, validate_prices,
)
from notifier import save_alert
import yaml


def try_sync_portfolio():
    """Attempt to sync portfolio from Schwab. Silently skip if not configured."""
    try:
        from brokerage import sync_portfolio
        sync_portfolio()
        print("  Portfolio synced from Schwab")
    except (ImportError, SystemExit):
        pass  # schwab-py not installed or not configured
    except Exception as e:
        print(f"  [!] Portfolio sync failed (using manual portfolio): {e}")

ROOT = Path(__file__).parent.parent


def load_config():
    with open(ROOT / "config" / "sources.yaml") as f:
        return yaml.safe_load(f)


# ── Concise alert formatting ──────────────────────────────────────────

def _trunc(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def _confidence_color(confidence: str) -> int:
    key = confidence.split("-")[0] if "-" in confidence else confidence
    return {"high": 0x2ECC71, "medium": 0xF39C12, "low": 0x95A5A6}.get(key, 0x95A5A6)


def build_alert_embed(analysis: dict) -> discord.Embed:
    """Build the entire alert as one compact, scannable embed."""

    # Situation summary — 2-3 sentences
    situation = analysis.get("situation_summary", "")
    if not situation:
        # Fallback: join first few events
        events = analysis.get("events_analyzed", [])
        situation = ". ".join(events[:3])

    # Trade ideas — one line each
    ideas = analysis.get("trade_ideas", [])
    trade_lines = []
    for i, idea in enumerate(ideas):
        direction = idea.get("direction", "").upper()
        instrument = idea.get("instrument", "")
        # Use the one_liner if available, otherwise first sentence of thesis
        one_liner = idea.get("one_liner", "")
        if not one_liner:
            thesis = idea.get("thesis", "")
            one_liner = thesis.split(".")[0] + "." if "." in thesis else thesis[:80]
        horizon = idea.get("time_horizon", "")
        # Keep it short
        if horizon:
            trade_lines.append(f"**{i+1}. {direction} {instrument}**\n> {one_liner} *({horizon})*")
        else:
            trade_lines.append(f"**{i+1}. {direction} {instrument}**\n> {one_liner}")

    desc = f"{situation}\n\n" + "\n\n".join(trade_lines)

    embed = discord.Embed(
        title="THALAMUS ALERT",
        description=_trunc(desc, 4096),
        color=0xE74C3C,
        timestamp=datetime.now(timezone.utc),
    )
    return embed


def build_trade_detail_embed(idea: dict) -> discord.Embed:
    """Build a full detail embed for when user clicks 'Details'."""
    direction = idea.get("direction", "").upper()
    instrument = idea.get("instrument", "")
    confidence = idea.get("confidence", "?")
    horizon = idea.get("time_horizon", "?")

    # Chain as the main content — vertical numbered steps
    chain = idea.get("chain", [])
    chain_lines = []
    for i, step in enumerate(chain):
        if i < len(chain) - 1:
            chain_lines.append(f"{i+1}. {step}")
            chain_lines.append(f"   ↓")
        else:
            chain_lines.append(f"**{i+1}. {step}**")

    # Thesis paragraph, then chain, then counter
    parts = []
    parts.append(idea.get("thesis", ""))
    if chain_lines:
        parts.append(f"\n**How it plays out:**\n" + "\n".join(chain_lines))

    counter = idea.get("counter_thesis", "")
    if counter:
        parts.append(f"\n**What could go wrong:**\n{counter}")

    embed = discord.Embed(
        title=f"{direction}  {instrument}",
        description=_trunc("\n".join(parts), 4096),
        color=_confidence_color(confidence),
    )
    embed.set_footer(text=f"{confidence} confidence  |  {horizon}")
    return embed


# ── Button views ──────────────────────────────────────────────────────

class TradeDetailButton(ui.Button):
    def __init__(self, idea: dict, index: int):
        instrument = idea.get("instrument", "")
        # Extract just ticker from instrument string e.g. "CF Industries (CF) — blah" -> "CF"
        ticker = instrument
        if "(" in instrument and ")" in instrument:
            ticker = instrument.split("(")[1].split(")")[0]
        elif " " in instrument:
            ticker = instrument.split(" ")[0]
        label = f"{ticker} Details"[:80]
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=0)
        self.idea = idea

    async def callback(self, interaction: discord.Interaction):
        embed = build_trade_detail_embed(self.idea)
        await interaction.response.send_message(embed=embed)


class AlertView(ui.View):
    def __init__(self, analysis: dict):
        super().__init__(timeout=None)  # Buttons don't expire
        ideas = analysis.get("trade_ideas", [])
        for i, idea in enumerate(ideas[:5]):
            self.add_item(TradeDetailButton(idea, i))


# ── Discord bot ───────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
anthropic_client = None
alert_channel = None
_announced = False


def get_chat_context() -> str:
    """Build context string for chat responses."""
    world_model = load_world_model()
    portfolio = load_portfolio()

    # Load recent alerts
    alerts_dir = ROOT / "memory" / "alerts"
    recent_alerts = []
    if alerts_dir.exists():
        alert_files = sorted(alerts_dir.iterdir(), reverse=True)[:3]
        for f in alert_files:
            try:
                alert = json.loads(f.read_text())
                # Just include trade ideas, not the full narrative
                ideas = alert.get("trade_ideas", [])
                for idea in ideas:
                    recent_alerts.append(
                        f"- {idea.get('direction', '').upper()} {idea.get('instrument', '')}: "
                        f"{idea.get('thesis', '')[:200]}"
                    )
            except Exception:
                pass

    alerts_text = "\n".join(recent_alerts) if recent_alerts else "(No recent alerts)"

    return f"""## Current World Model
{world_model[:12000]}

## Portfolio
{portfolio}

## Recent Trade Ideas
{alerts_text}"""


async def handle_chat(message: discord.Message):
    """Respond to a user message using Claude + world model context."""
    async with message.channel.typing():
        context = get_chat_context()

        def _call_api():
            return anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                tools=[{"type": "web_search_20250305"}],
                system="""You are Thalamus, a geopolitical intelligence agent. The user is a trader
who wants to discuss your analysis and world model. Be concise and direct.
Think in supply chains and second/third order effects. If they ask about a trade,
reason through it step by step. Use web search to look up current information
when the user asks about recent events, prices, or news.

Keep responses short — a few paragraphs max. Use bullet points.
Do not suggest obvious, consensus trades. Your value is non-obvious connections.""",
                messages=[{
                    "role": "user",
                    "content": f"""{context}

---

User question: {message.content}"""
                }]
            )

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _call_api)

        # Extract text blocks (web search tool results are handled server-side)
        text_parts = [block.text for block in response.content if hasattr(block, "text")]
        reply = "\n".join(text_parts)

        if not reply:
            reply = "I searched but couldn't find a useful answer. Try rephrasing?"

        # Discord message limit is 2000 chars
        if len(reply) > 1900:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            for chunk in chunks:
                await message.reply(chunk)
        else:
            await message.reply(reply)


def save_replay(timestamp: str, headlines: list, flagged: list, research: str):
    """Save cycle data for replay without re-fetching."""
    replay_dir = ROOT / "memory" / "replays"
    replay_dir.mkdir(parents=True, exist_ok=True)
    replay_file = replay_dir / f"{timestamp}.json"
    replay_file.write_text(json.dumps({
        "timestamp": timestamp,
        "headlines": headlines,
        "flagged": flagged,
        "research": research,
    }, indent=2))
    print(f"  Saved replay to {replay_file.name}")


def run_scan_cycle() -> dict | None:
    """Execute one scan cycle. Returns analysis result or None."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    print(f"\n{'='*60}")
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scan cycle")
    print(f"{'='*60}")

    config = load_config()
    scanner_model = config["models"]["scanner"]
    analyst_model = config["models"]["analyst"]

    # Step 0: Sync portfolio from brokerage (if configured)
    try_sync_portfolio()

    # Step 1: Pull headlines
    print("\n[1/6] Pulling RSS feeds...")
    headlines = pull_feeds(config)
    print(f"  Found {len(headlines)} new headlines")

    if not headlines:
        print("  Nothing new. Skipping cycle.")
        return None

    # Step 2: Classify
    print("\n[2/6] Classifying headlines...")
    world_model = load_world_model()
    model_summary = world_model[:3000] if len(world_model) > 3000 else world_model
    flagged = classify_headlines(anthropic_client, headlines, model_summary, scanner_model)
    print(f"  Flagged {len(flagged)} significant items")

    if not flagged:
        print("  Quiet cycle.")
        return None

    for item in flagged:
        print(f"  [{item['urgency'].upper()}] {item['title']}")

    # Prioritize
    high = [f for f in flagged if f["urgency"] == "high"]
    medium = [f for f in flagged if f["urgency"] == "medium"]
    low = [f for f in flagged if f["urgency"] == "low"]
    analysis_items = (high + medium + low)[:10]

    # Step 3: Research
    print("\n[3/6] Researching...")
    all_questions = []
    for item in analysis_items:
        all_questions.extend(item.get("follow_up_questions", []))

    research = ""
    if all_questions:
        research = research_questions(anthropic_client, all_questions[:5], analyst_model)

    # Save replay data (before analysis so we can re-analyze cheaply)
    save_replay(timestamp, headlines, analysis_items, research)

    # Step 4: Deep analysis
    print("\n[4/6] Analyzing...")
    portfolio = load_portfolio()
    result = deep_analysis(
        anthropic_client, analysis_items, world_model, portfolio, research, analyst_model
    )

    if "error" in result:
        print(f"  [!] Analysis error: {result['error']}")
        return None

    # Step 5: Update world model (always, even if no trade ideas)
    print("\n[5/6] Updating world model...")
    model_updates = result.get("world_model_updates", "")
    if model_updates:
        update_world_model(anthropic_client, world_model, model_updates, analyst_model)

    # Step 6: Price validation (only if there are trade ideas)
    trade_ideas = result.get("trade_ideas", [])
    if trade_ideas and result.get("alert_worthy", False):
        print("\n[6/6] Checking prices to validate ideas...")
        result["trade_ideas"] = validate_prices(
            anthropic_client, trade_ideas, analyst_model
        )
    else:
        print("\n[6/6] No actionable ideas this cycle — world model updated quietly.")

    save_alert(result)
    print("Cycle complete.")
    return result


async def generate_idle_message() -> str:
    """Generate a one-liner for quiet cycles."""
    def _call():
        return anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system="You are Thalamus, a geopolitical intelligence agent that monitors world events for trading opportunities. You just finished scanning headlines and found nothing interesting. Say something in one sentence — be weird, funny, cryptic, philosophical, or darkly humorous. No emojis. Don't mention that you're an AI. You can reference geopolitics, markets, supply chains, or just be strange. Keep it under 200 characters.",
            messages=[{"role": "user", "content": "What's on your mind?"}],
        )
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, _call)
    return response.content[0].text


async def scan_loop():
    """Background task that runs the scan cycle periodically."""
    await client.wait_until_ready()

    # Wait before first scan — prevents spam if bot is crash-restarting
    await asyncio.sleep(120)

    config = load_config()
    interval_hours = config.get("scan_interval_hours", 4)
    interval_seconds = interval_hours * 3600

    while not client.is_closed():
        try:
            # Run scan in thread pool to not block the bot
            result = await asyncio.get_event_loop().run_in_executor(None, run_scan_cycle)

            if result and result.get("alert_worthy", False) and alert_channel:
                embed = build_alert_embed(result)
                view = AlertView(result)
                await alert_channel.send(embed=embed, view=view)
            elif alert_channel:
                # Quiet cycle — say something weird
                msg = await generate_idle_message()
                await alert_channel.send(msg)

        except Exception as e:
            print(f"[!] Scan cycle error: {e}")
            import traceback
            traceback.print_exc()

        await asyncio.sleep(interval_seconds)


@client.event
async def on_ready():
    global alert_channel, anthropic_client, _announced
    anthropic_client = Anthropic()

    print(f"Thalamus bot logged in as {client.user}")

    # Find the alerts channel
    channel_name = os.environ.get("DISCORD_CHANNEL_NAME", "alerts")
    for guild in client.guilds:
        for ch in guild.text_channels:
            if ch.name == channel_name:
                alert_channel = ch
                print(f"Alert channel: #{ch.name} in {guild.name}")
                break

    if not alert_channel:
        print(f"[!] Could not find #{channel_name} channel!")
        print(f"    Available channels: {[ch.name for g in client.guilds for ch in g.text_channels]}")

    # Send startup message (only once per process — on_ready can fire on reconnects)
    if alert_channel and not _announced:
        _announced = True
        await alert_channel.send("**Thalamus online.** Scanning every 4 hours. Type here to ask me anything.")

    # Start the scan loop
    if not hasattr(client, '_scan_started'):
        client._scan_started = True
        client.loop.create_task(scan_loop())


@client.event
async def on_message(message: discord.Message):
    # Ignore own messages
    if message.author == client.user:
        return

    # Only respond in the alerts channel
    if alert_channel and message.channel.id == alert_channel.id:
        await handle_chat(message)


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN not set")
        return
    client.run(token)


if __name__ == "__main__":
    main()
