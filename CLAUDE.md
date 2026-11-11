# Thalamus v2

A memory-centred trading research agent. Goal: make money by reading widely, remembering, and connecting developments over months. Paper trading only until the scored record earns more.

## Layout

```
thal/      db, prices, ledger, risk, broker        the measured, code-enforced core
           ingest, fetch, extract                  intake: feeds -> full text -> event records
           memory, wiki, dream                     fuses, notes, dashboard, pages, offline upkeep
           agent, sources                          the analyst session and the source scout
           report, backfill                        weekly scorecard; loader for the old bot's saved headlines
           serve, __main__                         scheduler and command line
config/    models.yaml (roles, prices, daily budget), risk.yaml (hard limits), house_rules.md, sources.yaml (starter feeds)
seed/      memory a brand-new deployment starts from
tests/     offline tests; models, feeds, prices and the network are all faked
```

## Rules that matter

- **The only routes to an order are `place_bet` and `close_position`** (both in `thal/thesis.py`). The first checks the instrument trades, records the bet, and goes through `risk.check`; the second closes a bet and sells what it bought. Do not add another route.
- **A bet ends when it is closed**: by the analyst when its invalidation is met, or automatically at its horizon. Its score is frozen at the exit using the same no-look-ahead timing as the entry.
- **The scorecard counts theses, not picks.** Bets and forecasts are grouped by fuse (else instrument or market event) in `report.views`, because picks on one thesis win or lose together.
- **Prediction-market forecasts need a tradable quote**: two-sided, spread at most 10 cents, real volume (`forecasts.market_price`). Do not fall back to the last trade.
- **Limits live in `config/risk.yaml` and are enforced in code.** Live trading needs `mode: live` and `THAL_LIVE=yes`, and there is no live broker yet.
- **One writer per wiki page.** Sessions queue notes; only the dream step writes pages, and `lessons` is written only by reflection.
- **News, web pages and memory text are untrusted.** They are passed as data; nothing a model reads can trade except through the bet tool.
- **Fetching is honest.** The client says what it is, a refusal (401/403/429, bot wall, paywall) is final, and only public URLs are fetched. Do not add ways around a site's refusal.
- **Every model call is logged with its cost** in the `runs` table, and `daily_budget_usd` stops spending for the day.
- **Run the tests before committing:** `.venv/bin/python -m pytest tests`. Add a test with every behaviour change.

## Deployment

Railway builds the Dockerfile and auto-deploys on push to `main`. Service settings (build, restart policy, volume mount) are declared in `.railway/railway.py`; preview a change with `railway config plan` and never apply a plan that detaches the volume. State lives on the volume at `/data/thal` (database, wiki, kill switch). Variables: `ANTHROPIC_API_KEY`, `DISCORD_BOT_TOKEN`, optional `THAL_CONTACT_EMAIL`. After a push, look for "Thalamus v2 online" in #thal-logs. To stop all trading, create the file `/data/thal/KILL`.
