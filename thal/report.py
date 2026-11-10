"""The weekly scorecard: how the picks are doing, in plain numbers. No model calls.

Posted to Discord once a week and printed by `python -m thal report`. It answers one question:
is there evidence yet that the system knows something? Forecasts are judged against their
baselines, bets against their sector and the S&P.
"""
from __future__ import annotations

import sqlite3
import time

from . import forecasts, ledger

DAY = 86400.0
ENOUGH = 30          # below this many results a number is noise and is labelled so
V2 = "b.source = 'v2'"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.1f}%"


def _thin(n: int) -> str:
    return "" if n >= ENOUGH else " (too few to judge)"


def _forecast_lines(conn: sqlite3.Connection, now: float) -> list[str]:
    forecasts.ensure(conn)
    open_n = conn.execute("SELECT COUNT(*) FROM forecasts WHERE outcome IS NULL").fetchone()[0]
    week = conn.execute("SELECT COUNT(*) FROM forecasts WHERE resolved_at >= ?", (now - 7 * DAY,)).fetchone()[0]
    overall = forecasts.scores(conn)
    if not overall:
        return [f"None resolved yet; {open_n} open."]
    line = lambda r: (f"{r['group']}: {r['n']} resolved, skill {r['skill']:+.0%} vs baseline "
                      f"(Brier {r['brier']:.3f} vs {r['baseline_brier']:.3f}), right side {r['hit']:.0%}{_thin(r['n'])}")
    out = [line(overall[0]) + f". {week} resolved this week, {open_n} open."]
    out += ["- " + line(r) for r in forecasts.scores(conn, "kind")]
    tags = sorted(forecasts.scores(conn, "tag"), key=lambda r: -r["n"])[:6]
    if tags:
        out.append("By topic: " + "; ".join(f"{r['group']} {r['skill']:+.0%} (n={r['n']})" for r in tags))
    return out


def _bet_lines(conn: sqlite3.Connection) -> list[str]:
    total = conn.execute("SELECT COUNT(*) FROM bets WHERE source = 'v2'").fetchone()[0]
    rows = ledger.scoreboard(conn, by="source", where=V2)
    if not rows:
        return [f"{total} recorded, none old enough to score yet."]
    out = [f"{total} recorded. Return signed by side; 'vs sector' is what counts as skill."]
    for r in rows:
        out.append(f"- {r['horizon']}: n={r['n']}, mean {_pct(r['mean'])}, vs sector {_pct(r['vs_bench'])}, "
                   f"vs S&P {_pct(r['vs_spy'])}, winners {r['win']:.0%}{_thin(r['n'])}")
    for by in ("side", "asset_class"):
        now_rows = [r for r in ledger.scoreboard(conn, by=by, where=V2) if r["horizon"] == "now"]
        if len(now_rows) > 1:
            out.append(f"To date by {by.replace('_', ' ')}: "
                       + "; ".join(f"{r['group']} {_pct(r['mean'])} (n={r['n']})" for r in now_rows))
    picks = conn.execute("SELECT b.side, b.symbol, m.ret FROM bets b JOIN marks m ON m.bet_id = b.id"
                         " WHERE b.source = 'v2' AND m.horizon = 'now' ORDER BY m.ret DESC").fetchall()
    if len(picks) >= 4:
        show = lambda p: f"{p['side'].lower()} {p['symbol']} {_pct(p['ret'])}"
        out.append("Best: " + ", ".join(show(p) for p in picks[:3]) + ". Worst: " + ", ".join(show(p) for p in picks[-3:][::-1]) + ".")
    return out


def _activity_line(conn: sqlite3.Connection, now: float) -> str:
    since = now - 7 * DAY
    one = lambda q, *a: conn.execute(q, a).fetchone()[0] or 0
    return (f"{one('SELECT COUNT(*) FROM events WHERE dup_of IS NULL AND item_id IN (SELECT id FROM items WHERE fetched_at >= ?)', since)} events from "
            f"{one('SELECT COUNT(*) FROM items WHERE fetched_at >= ?', since)} items, "
            f"{one('SELECT COUNT(*) FROM bets WHERE source = ? AND created_at >= ?', 'v2', since)} bets, "
            f"{one('SELECT COUNT(*) FROM forecasts WHERE created_at >= ?', since)} forecasts, "
            f"{one('SELECT COUNT(*) FROM fuses WHERE status = ?', 'open')} fuses open, "
            f"${one('SELECT SUM(cost) FROM runs WHERE at >= ?', since):.2f} spent.")


def scorecard(conn: sqlite3.Connection, now: float | None = None) -> str:
    now = now or time.time()
    return "\n".join([f"**Weekly scorecard, {time.strftime('%Y-%m-%d', time.gmtime(now))}**",
                      "**Forecasts** " + "\n".join(_forecast_lines(conn, now)),
                      "**Bets** " + "\n".join(_bet_lines(conn)),
                      "**This week** " + _activity_line(conn, now)])
