"""Load the old bot's alerts and red-team kills into the ledger so the scoreboard starts with history.

Usage: python -m tools.backfill_v1 data/v1_ideas.json
(ideas file comes from tools/dump_discord.py then tools/parse_v1_discord.py)
"""
import datetime as dt
import json
import sys

from thal import db, ledger
from thal.prices import YahooProvider


def main(path: str) -> None:
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM bets WHERE source = 'v1'").fetchone()[0]:
        print("v1 history already loaded; skipping insert")
    else:
        n = 0
        for i in json.load(open(path)):
            sym = "FRO" if i["sym"] == "VLCC" else i["sym"]
            hedge = None
            if "soybeans / short corn" in i["text"]:
                sym, hedge = "ZS=F", "ZC=F"
            if not sym or i.get("skip"):
                continue
            ledger.add_bet(conn, symbol=sym, hedge_symbol=hedge, side=i["side"], source="v1",
                           kind=i["kind"].lower(), created_at=dt.datetime.fromisoformat(i["ts"]).timestamp(),
                           thesis=i["text"], benchmark="XLE")
            n += 1
        print(f"loaded {n} v1 ideas")
    print("marked", ledger.mark_all(conn, YahooProvider(conn)), "horizons")
    print(ledger.format_scoreboard(ledger.scoreboard(conn, by="kind", where="b.source = 'v1'")))


if __name__ == "__main__":
    main(sys.argv[1])
