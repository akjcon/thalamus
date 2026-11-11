"""The weekly scorecard: how the picks are doing, in plain numbers. No model calls.

Posted to Discord once a week and printed by `python -m thal report`. It answers one question:
is there evidence yet that the system knows something? Forecasts are judged against their
baselines, bets against their sector and the S&P.

Picks are not independent: several bets and forecasts often express one thesis and win or lose
together. So the scorecard also groups them into views (by fuse, else by instrument or
prediction-market event) and scores each view once. That number, not the pick count, says how
much evidence there is.
"""
from __future__ import annotations

import json
import re
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
    closed = conn.execute("SELECT COUNT(*) FROM bets WHERE source = 'v2' AND closed_at IS NOT NULL").fetchone()[0]
    rows = ledger.scoreboard(conn, by="source", where=V2)
    if not rows:
        return [f"{total} recorded, none old enough to score yet."]
    out = [f"{total} recorded, {closed} closed. Return signed by side; 'vs sector' is what counts as skill."]
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


FUSE_TAG = re.compile(r"^fuse-(\d+)$")


def _fuse_of(row) -> int | None:
    """The fuse a pick came from: recorded directly, or named in a tag like fuse-3."""
    if row["fuse_id"] is not None:
        return int(row["fuse_id"])
    for t in json.loads(row["tags"] or "[]"):
        m = FUSE_TAG.match(str(t))
        if m:
            return int(m.group(1))
    return None


def views(conn: sqlite3.Connection) -> dict[str, dict]:
    """Scored picks grouped by the thesis behind them: {view: {"bets": [value], "forecasts": [(brier, baseline brier)]}}.
    A bet's value is its return to date against its sector, or outright when it named none."""
    forecasts.ensure(conn)
    out: dict[str, dict] = {}
    slot = lambda key: out.setdefault(key, {"bets": [], "forecasts": []})
    by_symbol: dict[str, str] = {}
    for b in conn.execute("SELECT b.*, m.ret, m.vs_bench FROM bets b LEFT JOIN marks m ON m.bet_id = b.id AND m.horizon = 'now'"
                          " WHERE b.source = 'v2' ORDER BY b.id"):
        fuse = _fuse_of(b)
        key = f"fuse #{fuse}" if fuse else by_symbol.get(b["symbol"], f"{b['side'].lower()} {b['symbol']}")
        by_symbol.setdefault(b["symbol"], key)
        if b["ret"] is not None:
            slot(key)["bets"].append(b["vs_bench"] if b["vs_bench"] is not None else b["ret"])
    for f in conn.execute("SELECT * FROM forecasts WHERE outcome IS NOT NULL ORDER BY id"):
        fuse = _fuse_of(f)
        key = (f"fuse #{fuse}" if fuse else by_symbol.get(f["ref"]) or
               (f["ref"].rsplit("-", 1)[0] if f["kind"] == "market" and "-" in f["ref"] else f["ref"]))
        slot(key)["forecasts"].append(((f["prob"] - f["outcome"]) ** 2, (f["baseline"] - f["outcome"]) ** 2))
    return {k: v for k, v in out.items() if v["bets"] or v["forecasts"]}


def _view_lines(conn: sqlite3.Connection) -> list[str]:
    vs = views(conn)
    if not vs:
        return ["Nothing scored yet."]
    mean = lambda xs: sum(xs) / len(xs)
    picks = sum(len(v["bets"]) + len(v["forecasts"]) for v in vs.values())
    out = [f"{picks} scored picks rest on {len(vs)} separate theses. Each thesis counts once here."]
    fc = {k: (mean([a for a, _ in v["forecasts"]]), mean([b for _, b in v["forecasts"]])) for k, v in vs.items() if v["forecasts"]}
    if fc:
        base = mean([b for _, b in fc.values()])
        skill = 1 - mean([a for a, _ in fc.values()]) / base if base > 0 else 0.0
        out.append(f"- Forecasts: {len(fc)} views, skill {skill:+.0%}, {sum(a < b for a, b in fc.values())} of {len(fc)} beat the baseline{_thin(len(fc))}")
    bt = {k: mean(v["bets"]) for k, v in vs.items() if v["bets"]}
    if bt:
        out.append(f"- Bets: {len(bt)} views, mean {_pct(mean(list(bt.values())))} vs sector, "
                   f"{sum(x > 0 for x in bt.values())} of {len(bt)} ahead{_thin(len(bt))}")
    big = sorted(vs.items(), key=lambda kv: -(len(kv[1]["bets"]) + len(kv[1]["forecasts"])))[:3]
    if big and len(big[0][1]["bets"]) + len(big[0][1]["forecasts"]) > 1:
        def show(k, v):
            title = ""
            if k.startswith("fuse #"):
                row = conn.execute("SELECT title FROM fuses WHERE id = ?", (int(k[6:]),)).fetchone()
                title = f" ({row['title'][:45]})" if row else ""
            bits = ([f"{len(v['bets'])} bets {_pct(bt[k])}"] if v["bets"] else []) + \
                   ([f"{len(v['forecasts'])} forecasts {'ahead of' if fc[k][0] < fc[k][1] else 'behind'} baseline"] if v["forecasts"] else [])
            return f"{k}{title}: " + ", ".join(bits)
        out.append("Most picks: " + "; ".join(show(k, v) for k, v in big))
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
                      "**Independent views** " + "\n".join(_view_lines(conn)),
                      "**This week** " + _activity_line(conn, now)])
