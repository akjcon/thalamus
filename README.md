# thalamus

An agent that reads a lot of news, remembers what matters, and tries to connect new developments to things it learned weeks or months earlier. It records every idea as a bet and scores the bets against real prices, so it can find out which kinds of connection are worth anything.

It trades on paper only. Treat everything it outputs as an experiment, not financial advice.

## How it works

- **Intake.** Pulls feeds, fetches the full article behind each item, and has a small model turn each one into a structured event record. The client identifies itself and takes a site's refusal as final.
- **Memory.** An append-only event log; fuses (open hypotheses, each with the evidence that would confirm or kill it); a wiki of linked pages with one writer per page; and a small dashboard that every session starts from.
- **Analyst.** One session a day with tools: it reads the new events, searches memory and the web, checks prices, updates fuses, and proposes bets.
- **Ledger.** Every bet is scored at 1, 4 and 12 weeks against the S&P 500 and a sector benchmark. Shadow bets are scored without placing an order.
- **Limits.** Orders pass through hard limits enforced in plain code. Paper mode is the default and there is a kill switch.
- **Scout.** A weekly session that searches for new sources, puts them on trial, and retires ones that never lead anywhere. Sources are scored by what their items led to.
- **Dream.** A nightly job that folds notes into wiki pages and, weekly, rewrites a short lessons page from the scored record.

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
cp .env.example .env            # add ANTHROPIC_API_KEY
.venv/bin/python -m pytest tests
.venv/bin/python -m thal run    # one pass: intake, analyst, settle, mark
.venv/bin/python -m thal status
```

`python -m thal serve` runs forever on the built-in schedule; that is what the deployment runs.

## Stack

Python, SQLite, Claude (Haiku 5.5 for extraction, Opus 5.5 for note folding and scouting, Fable 5.2 for the analyst and reflection).
