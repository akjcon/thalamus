"""Command line: python -m thal <run|scout|dream|status|score|report|backfill|serve>

run     ingest feeds, extract events, run the analyst session, settle and mark
scout   search for new sources, trial them, retire ones that never pay
dream   offline memory maintenance: housekeeping, fold notes into wiki pages, rewrite lessons
status  print the dashboard, paper portfolio and spend
score   re-mark the ledger and print the scoreboard
report  print the weekly scorecard (forecasts vs baseline, bets vs sector)
backfill FOLDER [I N]  load the old bot's saved headline scans as history and extract events (shard I of N)
serve   run forever on the built-in schedule (what the deployment runs)
"""
from __future__ import annotations

import json
import sys
import time

from . import fetch as fetch_mod
from . import agent, backfill, db, dream, extract, forecasts, ingest, ledger, memory, report, risk, sources
from .broker import get_broker
from .llm import LLM, BudgetExceeded
from .prices import YahooProvider
from .wiki import Wiki


def run(conn, llm, wiki, provider, broker, now=None, fetch=None, feeds=None, fetcher=None) -> dict:
    now = now or time.time()
    rep = {"ingest": ingest.ingest(conn, feeds, fetch, now)}
    rep["fulltext"] = fetch_mod.fetch_pending(conn, **({"fetcher": fetcher, "sleep": lambda s: None} if fetcher else {}))
    rep["new_items"] = sum(v for v in rep.pop("ingest").values() if isinstance(v, int))
    try:
        rep["extract"] = extract.extract_pending(conn, llm, now=now)
        rep["analyst"] = agent.run_session(conn, llm, wiki, provider, broker, now)
    except BudgetExceeded as e:
        rep["stopped"] = str(e)
    rep["forecasts_resolved"] = forecasts.resolve(conn, provider, now=now)
    rep["orders_filled"] = broker.settle(now)
    rep["marks"] = ledger.mark_all(conn, provider, now)
    rep["spent_today"] = round(llm.spent_today(now), 3)
    return rep


def status(conn, llm, wiki, broker) -> str:
    pf = broker.portfolio()
    pos = ", ".join(f"{s} {q:g}" for s, q in pf.positions.items()) or "none"
    counts = {k: conn.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ("items", "events", "fuses", "bets", "orders")}
    return (f"{memory.dashboard(conn, wiki)}\n\n## Paper account\ncash ${pf.cash:,.0f} | equity ${pf.equity:,.0f} | "
            f"positions: {pos}\n\n## Counts\n{counts}\nspent today: ${llm.spent_today():.3f}")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "serve":
        from . import serve
        serve.main()
        return 0
    conn, wiki = db.connect(), Wiki()
    llm, provider = LLM(conn), YahooProvider(conn)
    broker = get_broker(conn, provider, risk.load())
    if cmd == "run":
        print(json.dumps(run(conn, llm, wiki, provider, broker), indent=1, default=str))
    elif cmd == "scout":
        print(json.dumps(sources.scout(conn, llm, wiki), indent=1, default=str))
        print(sources.stats_text(conn))
    elif cmd == "dream":
        print(json.dumps(dream.dream(conn, llm, wiki, provider, broker), indent=1, default=str))
    elif cmd == "score":
        ledger.mark_all(conn, provider)
        for by in ("source", "kind", "side"):
            print(f"\nby {by}\n" + ledger.format_scoreboard(ledger.scoreboard(conn, by=by)))
    elif cmd == "report":
        ledger.mark_all(conn, provider); forecasts.resolve(conn, provider)
        print(report.scorecard(conn))
    elif cmd == "backfill":
        if len(argv) < 3:
            print(__doc__)
            return 2
        shard = (int(argv[3]), int(argv[4])) if len(argv) > 4 else (0, 1)
        if shard[0] == 0:
            print(json.dumps(backfill.load(conn, argv[2])), flush=True)
        say = lambda d, n: print(f"{d['items']}/{n} items, {d['events']} events, ${llm.spent_today():.2f} spent today", flush=True)
        print(json.dumps(backfill.extract_backfill(conn, llm, shard=shard, progress=say)), flush=True)
        print(f"{backfill.pending(conn)} backfilled items still waiting", flush=True)
    elif cmd == "status":
        print(status(conn, llm, wiki, broker))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
