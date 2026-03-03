# Thalamus — Autonomous Geopolitical Intelligence Agent

## What This Is

Thalamus is a scan → classify → analyze → alert pipeline that monitors geopolitical events, maintains a dynamic world model, and identifies non-obvious second/third-order trading opportunities via Discord. It runs 24/7 on Railway.

## Project Structure

```
agent/          Core modules (bot, scanner, analyst, brokerage, replay, notifier)
config/         sources.yaml (RSS feeds + model config), agent_prompt.md (system prompt)
memory/         Persistent state — world_model/, alerts/, replays/, portfolio.md, seen_headlines.json
```

## Key Architecture

- **Scanner** (`scanner.py`): Pulls RSS feeds, deduplicates via SHA256 hash, classifies with **Haiku** (cheap/fast)
- **Analyst** (`analyst.py`): Deep analysis with **Sonnet** + web_search. Produces trade ideas + world model updates
- **Bot** (`bot.py`): Discord bot entry point. Runs scan loop every 4h, handles chat in #alerts channel
- **Notifier** (`notifier.py`): Formats analysis into Discord embeds with expandable trade detail buttons
- **Replay** (`replay.py`): Reruns analysis on saved cycle data without refetching headlines
- **Brokerage** (`brokerage.py`): Optional Schwab integration (read-only), auto-refreshes OAuth via Playwright

## Model Usage

| Task | Model | Why |
|------|-------|-----|
| Headline classification | `claude-haiku-4-5-20251001` | Binary decision, cheap |
| Deep analysis + world model updates | `claude-sonnet-4-6` | Complex reasoning |
| Research (web search) | `claude-sonnet-4-6` | Needs tool use |

## Trading Philosophy (from agent_prompt.md)

- NOT a news aggregator — finds non-obvious 2nd/3rd-order supply chain effects
- Most cycles should be **silent** — trade ideas are rare, that's correct behavior
- Form thesis from geopolitical first principles, THEN validate with price data (never the reverse)
- Think in physical commodity flows, shipping routes, input costs — not price charts
- Temporal edge: supply chain disruptions take weeks to propagate through inventories

## Running Locally

```bash
# One-time setup
cp .env.example .env  # Fill in API keys
pip install -r requirements.txt

# Run the Discord bot (production mode)
python3 agent/bot.py

# Run a single scan cycle (CLI)
python3 agent/main.py

# Replay a previous cycle
python3 agent/replay.py --list
python3 agent/replay.py 20260303_055901
```

## Deployment

- **Railway.app** — auto-deploys on push to main via native git integration
- **Persistent volume** mounted at `/data` — entrypoint.sh symlinks `memory/` → `/data/memory`
- **No CI/CD workflow** — Railway handles build + deploy directly

## Environment Variables

**Required:**
- `ANTHROPIC_API_KEY` — Claude API
- `DISCORD_BOT_TOKEN` — Discord bot auth
- `DISCORD_CHANNEL_NAME` — Channel to post alerts (default: `alerts`)

**Optional (Schwab):**
- `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_USERNAME`, `SCHWAB_PASSWORD`, `SCHWAB_CALLBACK_URL`

## Conventions

- All LLM JSON responses: strip markdown fences, parse with error handling, log first/last 500 chars on failure
- World model updates: Sonnet outputs `[{"action": "write", "filename": "...", "content": "..."}]` — Python executes ops
- RSS errors don't crash the cycle — logged and skipped
- Missing optional config (Schwab) degrades gracefully — silently skipped
- Paths use `pathlib.Path` with absolute references from project root
- Headlines deduped via first 16 chars of SHA256("title:link")
- Discord buttons use `ui.View(timeout=None)` for persistence
- Scan cycle runs blocking code in executor to not block Discord event loop
