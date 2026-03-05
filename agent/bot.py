"""
Thalamus Discord Bot — the main process.
Runs scan cycles on a timer and provides interactive alerts
with expandable trade ideas and chat capability.
"""

import os
import re
import json
import asyncio
import time
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
WORLD_MODEL_DIR = ROOT / "memory" / "world_model"


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
log_channel = None


# ── CycleLog — collects structured log entries during a scan cycle ────

class CycleLog:
    """Collects log entries during a scan cycle (runs in thread executor — no async)."""

    def __init__(self):
        self.entries = []
        self.start_time = time.monotonic()
        self.headline_count = 0
        self.flagged_count = 0
        self.novel_count = 0
        self.flagged_details = []
        self.verdict = "quiet"
        self.errors = []

    def log(self, msg: str):
        self.entries.append(msg)
        print(msg)  # Also print to stdout for Railway logs

    def error(self, msg: str):
        self.errors.append(msg)
        self.entries.append(f"[!] {msg}")
        print(f"[!] {msg}")

    def format_summary(self) -> str:
        elapsed = time.monotonic() - self.start_time
        lines = [f"**Scan cycle complete** ({elapsed:.0f}s)"]
        lines.append(f"Headlines: {self.headline_count} new | {self.flagged_count} flagged | {self.novel_count} novel")
        lines.append(f"Verdict: **{self.verdict}**")

        if self.flagged_details:
            lines.append("\n**Flagged:**")
            for d in self.flagged_details[:10]:
                lines.append(f"- [{d['urgency'].upper()}] {d['title'][:80]}")
                what_is_new = d.get('what_is_new', d.get('reason', ''))
                if what_is_new:
                    lines.append(f"  → {what_is_new[:120]}")

        if self.errors:
            lines.append("\n**Errors:**")
            for e in self.errors[:5]:
                lines.append(f"- {e[:200]}")

        return "\n".join(lines)


CHAT_TOOLS = [
    {"type": "web_search_20250305", "name": "web_search"},
    {
        "name": "read_world_model",
        "description": "Read a specific file from the world model. Use this to get detailed analysis on a topic. Available files are listed in the system prompt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "The filename to read (e.g., 'energy_supply_chains.md', 'oil_gas_market.md')"
                }
            },
            "required": ["filename"]
        }
    }
]


def execute_tool(name: str, tool_input: dict) -> str:
    """Handle client-side tool calls from chat harness."""
    if name == "read_world_model":
        filename = tool_input.get("filename", "")
        filepath = WORLD_MODEL_DIR / filename
        print(f"  [chat] read_world_model({filename})")
        if filepath.exists() and filepath.suffix == ".md":
            return filepath.read_text()
        return f"File not found: {filename}"
    return f"Unknown tool: {name}"


# ── Chat conversation memory ─────────────────────────────────────────
# Keeps recent conversation history so follow-up questions work.
# Keyed by channel ID. Each entry is a list of {role, content} dicts.
# Expires after 30 minutes of inactivity.

_chat_history: dict[int, list[dict]] = {}
_chat_last_active: dict[int, float] = {}
CHAT_HISTORY_MAX = 20  # max messages to keep (user + assistant)
CHAT_HISTORY_TTL = 1800  # 30 minutes


def get_chat_history(channel_id: int) -> list[dict]:
    """Get conversation history for a channel, clearing if stale."""
    last = _chat_last_active.get(channel_id, 0)
    if time.monotonic() - last > CHAT_HISTORY_TTL:
        _chat_history.pop(channel_id, None)
    return _chat_history.get(channel_id, [])


def append_chat_history(channel_id: int, role: str, content: str):
    """Append a message to conversation history."""
    if channel_id not in _chat_history:
        _chat_history[channel_id] = []
    _chat_history[channel_id].append({"role": role, "content": content})
    # Trim to max length (keep most recent)
    if len(_chat_history[channel_id]) > CHAT_HISTORY_MAX:
        _chat_history[channel_id] = _chat_history[channel_id][-CHAT_HISTORY_MAX:]
    _chat_last_active[channel_id] = time.monotonic()


async def handle_chat(message: discord.Message):
    """Respond to a user message using Claude with tool-calling loop."""
    print(f"  [chat] handle_chat called: {message.content[:80]}", flush=True)
    async with message.channel.typing():
        # Build system prompt with world model index (not full content)
        index_path = WORLD_MODEL_DIR / "_index.md"
        index_content = index_path.read_text() if index_path.exists() else "(No world model index)"
        portfolio = load_portfolio()

        # Recent trade ideas (small, include directly)
        alerts_dir = ROOT / "memory" / "alerts"
        recent_alerts = []
        if alerts_dir.exists():
            for f in sorted(alerts_dir.iterdir(), reverse=True)[:3]:
                try:
                    alert = json.loads(f.read_text())
                    for idea in alert.get("trade_ideas", []):
                        recent_alerts.append(
                            f"- {idea.get('direction', '').upper()} {idea.get('instrument', '')}: "
                            f"{idea.get('thesis', '')[:200]}"
                        )
                except Exception:
                    pass
        alerts_text = "\n".join(recent_alerts) if recent_alerts else "(No recent alerts)"

        system_prompt = f"""You are Thalamus, a geopolitical intelligence agent advising a trader. \
Think in supply chains and second/third-order effects.

RULES:
1. You HAVE web search and world model tools. Use them BEFORE generating any response text. \
NEVER say "I don't have real-time data" or "I can't check" — you CAN and MUST. \
Do NOT generate preamble text before searching. Search first, then respond with findings.
2. Use read_world_model to get your analysis on topics. Don't guess — read the file.
3. BREVITY IS MANDATORY. Max 4-6 bullet points or one short paragraph. No essays, no \
disclaimers, no "let me know if you want more detail", no follow-up questions unless \
critical context is missing. Just answer the question.
4. Your value is non-obvious connections, not consensus takes. Think physical commodity \
flows, input costs, shipping routes.

## World Model Index (use read_world_model to get full details)
{index_content}

## Portfolio
{portfolio}

## Recent Trade Ideas
{alerts_text}"""

        # Build messages from conversation history + new user message
        history = get_chat_history(message.channel.id)
        messages = list(history) + [{"role": "user", "content": message.content}]

        def _run_harness():
            max_turns = 10
            response = None
            for turn in range(max_turns):
                response = anthropic_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1200,
                    tools=CHAT_TOOLS,
                    system=system_prompt,
                    messages=messages,
                )
                if response.stop_reason == "end_turn":
                    break
                # Process tool_use blocks
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                if not tool_results:
                    break  # Only server-side tools used, response is complete
                print(f"  [chat] Turn {turn + 1}: {len(tool_results)} tool call(s)")
                messages.append({"role": "user", "content": tool_results})
            return response

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _run_harness)

        # Extract only post-tool text from the final response.
        # The model generates preamble text before tool use, then the real answer after.
        # Take text blocks that come after the last non-text block (tool use, search, etc).
        content = response.content
        block_types = [getattr(b, "type", type(b).__name__) for b in content]
        print(f"  [chat] Response blocks: {block_types}", flush=True)
        last_non_text_idx = -1
        for i, block in enumerate(content):
            if not hasattr(block, "text"):
                last_non_text_idx = i
        if last_non_text_idx >= 0:
            text_parts = [b.text for b in content[last_non_text_idx + 1:] if hasattr(b, "text")]
            print(f"  [chat] Stripped pre-tool text (last non-text at idx {last_non_text_idx})", flush=True)
        else:
            text_parts = [b.text for b in content if hasattr(b, "text")]
        reply = "\n".join(text_parts)

        if not reply:
            reply = "I searched but couldn't find a useful answer. Try rephrasing?"

        print(f"  [chat] Reply: {len(reply)} chars", flush=True)

        # Save to conversation history (user message + final text reply only)
        append_chat_history(message.channel.id, "user", message.content)
        append_chat_history(message.channel.id, "assistant", reply)

        # Cap at one Discord message — brevity is a feature
        if len(reply) > 1900:
            reply = reply[:1897] + "..."
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


def run_scan_cycle() -> tuple[dict | None, CycleLog]:
    """Execute one scan cycle. Returns (analysis result or None, cycle log)."""
    log = CycleLog()

    # Record scan start IMMEDIATELY — survives crashes, restarts, and quiet cycles
    record_scan_time()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log.log(f"\n{'='*60}")
    log.log(f"[{datetime.now(timezone.utc).isoformat()}] Starting scan cycle")
    log.log(f"{'='*60}")

    config = load_config()
    scanner_model = config["models"]["scanner"]
    analyst_model = config["models"]["analyst"]

    # Step 0: Sync portfolio from brokerage (if configured)
    try_sync_portfolio()

    # Step 1: Pull headlines
    log.log("[1/6] Pulling RSS feeds...")
    headlines = pull_feeds(config)
    log.headline_count = len(headlines)
    log.log(f"  Found {len(headlines)} new headlines")

    if not headlines:
        log.log("  Nothing new. Skipping cycle.")
        log.verdict = "no new headlines"
        return None, log

    # Step 2: Classify for novelty
    log.log("[2/6] Classifying headlines for novelty...")
    world_model = load_world_model()
    model_summary = world_model[:3000] if len(world_model) > 3000 else world_model
    flagged = classify_headlines(anthropic_client, headlines, model_summary, scanner_model)
    log.flagged_count = len(flagged)
    log.flagged_details = flagged
    log.log(f"  Flagged {len(flagged)} novel items")

    if not flagged:
        log.log("  Quiet cycle — nothing novel.")
        log.verdict = "quiet — no novel headlines"
        return None, log

    for item in flagged:
        what_is_new = item.get('what_is_new', item.get('reason', ''))
        log.log(f"  [{item['urgency'].upper()}] {item['title']}")
        if what_is_new:
            log.log(f"    → {what_is_new}")

    # Prioritize
    high = [f for f in flagged if f["urgency"] == "high"]
    medium = [f for f in flagged if f["urgency"] == "medium"]
    low = [f for f in flagged if f["urgency"] == "low"]
    analysis_items = (high + medium + low)[:10]
    log.novel_count = len(analysis_items)

    # Step 3: Research
    log.log("[3/6] Researching...")
    all_questions = []
    for item in analysis_items:
        all_questions.extend(item.get("follow_up_questions", []))

    research = ""
    if all_questions:
        research = research_questions(anthropic_client, all_questions[:5], analyst_model)

    # Save replay data (before analysis so we can re-analyze cheaply)
    save_replay(timestamp, headlines, analysis_items, research)

    # Step 4: Deep analysis
    log.log("[4/6] Analyzing...")
    portfolio = load_portfolio()
    result = deep_analysis(
        anthropic_client, analysis_items, world_model, portfolio, research, analyst_model
    )

    if "error" in result:
        log.error(f"Analysis error: {result['error']}")
        log.verdict = "error in analysis"
        return None, log

    # Step 5: Update world model (always, even if no trade ideas)
    log.log("[5/6] Updating world model...")
    model_updates = result.get("world_model_updates", "")
    if model_updates:
        update_world_model(anthropic_client, world_model, model_updates, analyst_model)

    # Step 6: Price validation (only if there are trade ideas)
    trade_ideas = result.get("trade_ideas", [])
    if trade_ideas and result.get("alert_worthy", False):
        log.log("[6/6] Checking prices to validate ideas...")
        result["trade_ideas"] = validate_prices(
            anthropic_client, trade_ideas, analyst_model
        )
    else:
        log.log("[6/6] No actionable ideas this cycle — world model updated quietly.")

    # Programmatic dedup — filter out instruments already recommended recently
    if result.get("trade_ideas"):
        result["trade_ideas"] = filter_repeat_ideas(result["trade_ideas"])
        if not result["trade_ideas"]:
            log.log("  All ideas were repeats — downgrading to quiet cycle.")
            result["alert_worthy"] = False

    if result.get("alert_worthy"):
        save_alert(result)
        ideas = result.get("trade_ideas", [])
        tickers = []
        for idea in ideas:
            found = re.findall(r'\(([A-Z]{1,5})\)', idea.get("instrument", ""))
            tickers.extend(found)
        log.verdict = f"ALERT — {', '.join(tickers)}" if tickers else "ALERT"
    else:
        log.verdict = "quiet — world model updated, no new trades"

    log.log("Cycle complete.")
    return result, log


def last_scan_time() -> datetime | None:
    """Check when the last scan started by reading a simple marker file."""
    scan_marker = ROOT / "memory" / "last_scan.txt"
    if not scan_marker.exists():
        return None
    try:
        return datetime.fromisoformat(scan_marker.read_text().strip())
    except (ValueError, OSError):
        return None


def record_scan_time():
    """Write current UTC time to the scan marker file."""
    scan_marker = ROOT / "memory" / "last_scan.txt"
    scan_marker.parent.mkdir(parents=True, exist_ok=True)
    scan_marker.write_text(datetime.now(timezone.utc).isoformat())


def filter_repeat_ideas(trade_ideas: list[dict]) -> list[dict]:
    """Remove trade ideas for instruments already recommended recently."""
    alerts_dir = ROOT / "memory" / "alerts"
    if not alerts_dir.exists():
        return trade_ideas

    # Collect tickers from recent alerts (wide window — ~3 days at 6h cycles)
    recent_tickers = set()
    for f in sorted(alerts_dir.iterdir(), reverse=True)[:15]:
        try:
            alert = json.loads(f.read_text())
            for idea in alert.get("trade_ideas", []):
                instrument = idea.get("instrument", "")
                tickers = re.findall(r'\(([A-Z]{1,5})\)', instrument)
                recent_tickers.update(tickers)
        except Exception:
            pass

    if not recent_tickers:
        return trade_ideas

    filtered = []
    for idea in trade_ideas:
        instrument = idea.get("instrument", "")
        idea_tickers = re.findall(r'\(([A-Z]{1,5})\)', instrument)
        overlap = set(idea_tickers) & recent_tickers
        if overlap:
            print(f"  [*] Filtering repeat idea: {', '.join(overlap)} (already recommended)", flush=True)
            continue
        filtered.append(idea)

    return filtered


async def send_to_log_channel(message: str):
    """Send a message to the log channel, truncating if needed."""
    if not log_channel:
        return
    # Discord limit is 2000 chars
    if len(message) > 1900:
        message = message[:1900] + "\n..."
    try:
        await log_channel.send(message)
    except Exception as e:
        print(f"[!] Failed to send to log channel: {e}")


async def scan_loop():
    """Background task that runs the scan cycle periodically."""
    await client.wait_until_ready()

    config = load_config()
    interval_hours = config.get("scan_interval_hours", 4)

    while not client.is_closed():
        # Check if enough time has passed since last scan (survives restarts + disconnects)
        last = last_scan_time()
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            remaining = (interval_hours * 3600) - elapsed
            if remaining > 0:
                print(f"  Last scan was {elapsed/60:.0f}m ago, next in {remaining/60:.0f}m")
                await asyncio.sleep(min(remaining, 300))  # Re-check every 5 min or when due
                continue

        try:
            # Run scan in thread pool to not block the bot
            result, cycle_log = await asyncio.get_event_loop().run_in_executor(None, run_scan_cycle)

            # Always log to #thal-logs
            await send_to_log_channel(cycle_log.format_summary())

            # Only send to #alerts if genuinely new trade ideas
            if result and result.get("alert_worthy", False) and alert_channel:
                embed = build_alert_embed(result)
                view = AlertView(result)
                await alert_channel.send(embed=embed, view=view)

        except Exception as e:
            print(f"[!] Scan cycle error: {e}")
            import traceback
            traceback.print_exc()
            await send_to_log_channel(f"**Scan cycle error:** {e}")

        await asyncio.sleep(60)


@client.event
async def on_ready():
    global alert_channel, log_channel, anthropic_client
    anthropic_client = Anthropic()

    print(f"Thalamus bot logged in as {client.user}")

    # Find channels
    alert_channel_name = os.environ.get("DISCORD_CHANNEL_NAME", "alerts")
    log_channel_name = os.environ.get("DISCORD_LOG_CHANNEL_NAME", "thal-logs")

    for guild in client.guilds:
        for ch in guild.text_channels:
            if ch.name == alert_channel_name and not alert_channel:
                alert_channel = ch
                print(f"Alert channel: #{ch.name} in {guild.name}")
            if ch.name == log_channel_name and not log_channel:
                log_channel = ch
                print(f"Log channel: #{ch.name} in {guild.name}")

    if not alert_channel:
        print(f"[!] Could not find #{alert_channel_name} channel!")
        print(f"    Available channels: {[ch.name for g in client.guilds for ch in g.text_channels]}")
    if not log_channel:
        print(f"[!] Could not find #{log_channel_name} channel — logs will only go to stdout")

    # Startup message to log channel only (not #alerts)
    if log_channel and not hasattr(client, '_announced'):
        client._announced = True
        await log_channel.send("**Thalamus online.** Scanning every 4 hours.")

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
        # Manual scan trigger
        if message.content.strip().lower() == "!scan":
            await message.reply("Starting scan cycle...")
            try:
                result, cycle_log = await asyncio.get_event_loop().run_in_executor(None, run_scan_cycle)
                await send_to_log_channel(cycle_log.format_summary())
                if result and result.get("alert_worthy", False):
                    embed = build_alert_embed(result)
                    view = AlertView(result)
                    await message.channel.send(embed=embed, view=view)
                else:
                    await message.reply(f"Scan complete: {cycle_log.verdict}")
            except Exception as e:
                await message.reply(f"Scan error: {e}")
            return
        await handle_chat(message)


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN not set")
        return
    client.run(token)


if __name__ == "__main__":
    main()
