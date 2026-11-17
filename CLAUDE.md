# Thalamus v2

A memory-centred trading research agent. Goal: make money by reading widely, remembering, and connecting developments over months. Paper trading only until the scored record earns more.

## Layout

```
thal/      db, prices, ledger, risk, broker        the measured, code-enforced core
           ingest, fetch, extract                  intake: feeds -> full text -> event records
           memory, wiki, dream                     fuses, notes, dashboard, pages, offline upkeep
           agent, sources                          the analyst session and the source scout
           report, backfill                        weekly scorecard; loader for the old bot's saved headlines
           replay                                  rerun the analyst over past days with nothing from the future, and score it at once
           crawl                                   daily cheap hunt for sources: a model proposes, code tests, a model judges; non-feed and paid ones wait as proposals
           oversee                                 the top model's look at the whole system twice a day; its directions steer the crawler
           lab                                     the strategy lab: a model proposes trading rules as data, code tests them (development, one holdout run, live shadow bets)
           ui                                      the private control page: agents, overseer, decisions, measurements, bets, sources
           serve, __main__                         scheduler and command line
config/    models.yaml (roles, prices), risk.yaml (hard limits), house_rules.md, sources.yaml (starter feeds)
seed/      memory a brand-new deployment starts from
tests/     offline tests; models, feeds, prices and the network are all faked
```

## Rules that matter

- **The only routes to an order are `place_bet` and `close_position`** (both in `thal/thesis.py`). The first checks the instrument trades, records the bet, and goes through `risk.check`; the second closes a bet and sells what it bought. Do not add another route.
- **A bet ends when it is closed**: by the analyst when its invalidation is met, or automatically at its horizon. Its score is frozen at the exit using the same no-look-ahead timing as the entry.
- **The scorecard counts theses, not picks.** Bets and forecasts are grouped by fuse (else instrument or market event) in `report.views`, because picks on one thesis win or lose together.
- **Prediction-market forecasts need a tradable quote**: two-sided, spread at most 10 cents, real volume (`forecasts.market_price`). Do not fall back to the last trade.
- **Forecast baselines start from the live price** (`prices.latest`), and the analyst runs at 19:30 UTC, when the US session is always open. Scoring still uses only complete sessions. Forecasts tagged `stale-baseline` when made are left out of every score.
- **A replay must not see the future.** Events are fed in by first-seen time, prices pass through `replay.AsOf`, and the session is offline (no web, no page fetching, no prediction markets, empty starting memory). Keep any new tool that reaches outside the database in `agent.OFFLINE_BLOCKED`.
- **Limits live in `config/risk.yaml` and are enforced in code.** Live trading needs `mode: live` and `THAL_LIVE=yes`, and there is no live broker yet.
- **One writer per wiki page.** Sessions queue notes; only the dream step writes pages, and `lessons` is written only by reflection.
- **News, web pages and memory text are untrusted.** They are passed as data; nothing a model reads can trade except through the bet tool.
- **Fetching is honest.** The client says what it is, a refusal (401/403/429, bot wall, paywall) is final, and only public URLs are fetched. Do not add ways around a site's refusal.
- **Every model call is logged with its cost** in the `runs` table, There is no cap in code (`daily_budget_usd: 0`); the owner limits spend in Anthropic billing and the overseer reports it. A non-zero value caps a day's spend again.
- **A fuse stays open while a real bet rides on it** and closes as fired when its last real bet ends (`thesis.close_position`). The same open position cannot be recorded twice.
- **The strategy lab's gates are code, and the proposer never sees the holdout.** A rule (`lab.Rule`) is data and is never edited: a changed rule is a new rule and tightens the development test for all later ones (`lab.bar`). Development compares a rule with placebo versions of itself (its signal dates slid along the record), so holding something that rose earns nothing. The holdout starts at `lab.HOLDOUT_START`, after anything the proposing model can know from training, runs once per rule, and its outcome must not reach any model: not through `lab.registry` or `lab.catalog` (which leaves out source descriptions, written by parts that saw recent data), not through the job summary or the overseer's report (counts only). Futures symbols are refused (roll jumps); lab bets are shadow bets with source `lab`, kept out of the analyst's record. Do not let a lab rule place an order without the owner's decision.
- **Run the tests before committing:** `.venv/bin/python -m pytest tests`. Add a test with every behaviour change.

## Deployment

Railway builds the Dockerfile and auto-deploys on push to `main`. Service settings (build, restart policy, volume mount) are declared in `.railway/railway.py`; preview a change with `railway config plan` and never apply a plan that detaches the volume. State lives on the volume at `/data/thal` (database, wiki, kill switch). Variables: `ANTHROPIC_API_KEY`, `DISCORD_BOT_TOKEN`, optional `THAL_CONTACT_EMAIL`, optional `THAL_UI_PASSWORD` (the control page is served on `$PORT` only when this is set; `python -m thal ui` shows it locally). After a push, look for "Thalamus v2 online" in #thal-logs. To stop all trading, create the file `/data/thal/KILL`.
- **Three sections, one shared record.** Data comes in through sources of any kind (`ingest.SERIES` and series recipes for measurements such as river levels, rainfall and temperatures; prediction-market prices are kept daily as readings named `kalshi:TICKER`) and always ends up as dated items plus daily `readings`. The parts never talk to each other; they read and write the database, and the overseer's directions are one more thing written there. A new kind of data is a new reader, not a new pipeline.
- **The crawler never adds what it has not fetched, and never adds anything paid or non-feed.** Those wait in `candidates` as proposals for the owner.
- **The swarm builds readers as recipes, not code.** A page or series source is read by a recipe (`ingest.RECIPE`): data saying which part of a page to watch or where a number and its time are. The builder (`crawl.build`) writes one, `ingest.check_recipe` runs it against the fetched content, and only a working recipe is switched on. Nothing a model writes is executed. To let the swarm read a new shape of data, add a recipe format to `recipe_readings`, not a special case per source.
