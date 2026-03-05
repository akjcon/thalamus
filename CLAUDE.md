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
- **Bot** (`bot.py`): Discord bot entry point. Runs scan loop at fixed UTC times, handles chat in #alerts channel
- **Costs** (`costs.py`): Per-call API cost tracking. Posts cycle summaries to #thal-costs
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

## Git

- Commit atomically — one logical change per commit
- Use `/cp` to commit and push

## Engineering Philosophy

- When a solution is getting complex or hacky, stop and ask: is there a simpler way? Prefer leveraging existing state (files on disk, timestamps, etc.) over maintaining fragile in-memory state or timers.
- **Always enter plan mode for non-trivial changes.** This bot is in production, handling real trades. Multi-file changes, behavioral changes, prompt rewrites, and anything touching the scan/alert pipeline MUST go through plan mode first. Read the relevant code, understand the current state, propose the change, get approval. "Quick fixes" that skip planning have repeatedly caused deploy failures and broken behavior. The only exception is true one-liners (typo fix, config value change).

## Cost Discipline

- **Every API call costs real money.** Before adding or modifying an API call, check: what model, what max_tokens, how often does this run? A 32K max_tokens call on Sonnet 4 times/day is ~$120/month by itself.
- **web_search is expensive.** Server-managed web search injects large result content into context. Always set `max_uses` (3 is a good default). In multi-turn tool loops, each turn re-sends all previous search results, so limit `max_turns` too.
- **Never use Opus in automated pipelines** without explicit approval. Opus output tokens are 5x Sonnet. Reserve it for one-off deep analysis, not recurring cycles.
- **Cost tracking exists** — `agent/costs.py` tracks every API call. Check #thal-costs after deploys to verify costs match expectations. If a cycle costs more than ~$1.50, investigate.
- **Budget target: ~$4/day ($120/month).** 4 scan cycles at ~$0.70-1.00 each + chat usage.

## Critical Deployment Rules

- **NEVER run bot.py locally while Railway is deployed.** Both instances use the same Discord token and Anthropic key. They will both process messages, both run scan loops, and double all API costs. The instance lock (`memory/instance.txt`) does NOT work across local/Railway because they have separate filesystems. If you must test locally, either stop the Railway service first or use `main.py` (CLI, no Discord) instead.
- **After pushing, verify Railway deploy succeeded** in #thal-logs (look for "Thalamus online" message). Don't assume the push worked.
- **Check `ps aux | grep bot.py`** before starting local development to ensure no zombie bot processes are running.

## Conventions

- All LLM JSON responses: strip markdown fences, parse with error handling, log first/last 500 chars on failure
- World model updates: Sonnet outputs `[{"action": "write", "filename": "...", "content": "..."}]` — Python executes ops
- RSS errors don't crash the cycle — logged and skipped
- Missing optional config (Schwab) degrades gracefully — silently skipped
- Paths use `pathlib.Path` with absolute references from project root
- Headlines deduped via first 16 chars of SHA256("title:link")
- Discord buttons use `ui.View(timeout=None)` for persistence
- Scan cycle runs blocking code in executor to not block Discord event loop
- Instance lock (`memory/instance.txt`) prevents deploy-overlap double responses on Railway — but only works on the shared volume, not across local/Railway
- Discord channels: #alerts (trade alerts + chat), #thal-logs (scan summaries), #thal-costs (API cost breakdowns)
- Chat costs are logged to stdout only (not #thal-costs) to avoid noise — scan cycle costs go to #thal-costs
- Module-level mutable state (like `costs._cost_log`) is NOT thread-safe across executor threads — keep scan and chat cost tracking separate
