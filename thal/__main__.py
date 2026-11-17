"""Command line: python -m thal <run|scout|dream|status|score|report|backfill|serve>

run     ingest feeds, extract events, run the analyst session, settle and mark
scout   search for new sources, trial them, retire ones that never pay
dream   offline memory maintenance: housekeeping, fold notes into wiki pages, rewrite lessons
status  print the dashboard, paper portfolio and spend
score   re-mark the ledger and print the scoreboard
report  print the weekly scorecard (forecasts vs baseline, bets vs sector)
backfill FOLDER [I N]  load the old bot's saved headline scans as history and extract events (shard I of N)
replay NAME START END BUDGET [EVERY [DREAM_EVERY]]  replay past days (YYYY-MM-DD) from data/replay/v1_events.db into
        data/replay/NAME, a session on every EVERYth day with news, stopping at BUDGET dollars; then print the score
        (a dream after every DREAM_EVERYth session; 0 means never, the no-memory control)
lab [propose]  run the strategy lab once: holdout tests and live rules; with "propose", ask a model for new rules first
serve   run forever on the built-in schedule (what the deployment runs)
ui [DB] show the control page at http://localhost:8787 for a database (default: the local one), on this machine only
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
    if cmd == "ui":
        from . import ui
        path = argv[2] if len(argv) > 2 else None
        ui.start_local(lambda: db.connect(path))
        print("control page on http://localhost:8787 (this machine only; Ctrl-C to stop)", flush=True)
        while True:
            time.sleep(3600)
    if cmd == "replay":
        from . import replay
        from .paths import ROOT
        if len(argv) < 6:
            print(__doc__)
            return 2
        base = ROOT / "data" / "replay"
        conn, src, llm, wiki, provider, broker = replay.open_replay(base / argv[2], base / "v1_events.db")
        say = lambda s: print(s, flush=True)
        rep = replay.run(conn, src, llm, wiki, provider, broker, start=replay.day_start(argv[3]), end=replay.day_start(argv[4]),
                         budget=float(argv[5]), every=int(argv[6]) if len(argv) > 6 else 1,
                         dream_every=int(argv[7]) if len(argv) > 7 else 1, say=say)
        say(json.dumps(rep))
        say(replay.score(conn, provider, broker))
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
    elif cmd == "lab":
        from . import lab
        print(lab.run(conn, llm, provider, proposing="propose" in argv[2:]))
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
