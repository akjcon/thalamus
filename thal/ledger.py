"""The ledger: every idea is recorded, then scored against real prices.

Nothing here calls a model. A bet is marked at 5, 20 and 60 sessions after entry and
"now", signed by side, raw and relative to SPY and to an optional sector benchmark.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time

from . import db
from .prices import Bar, PriceProvider, complete, entry_index, price_on_or_before

HORIZONS: list[tuple[str, int | None]] = [("1w", 5), ("4w", 20), ("12w", 60), ("now", None)]
MARKET = "SPY"


def asset_class(symbol: str) -> str:
    return "future" if symbol.endswith("=F") else "equity"


def add_bet(conn: sqlite3.Connection, *, symbol: str, side: str, source: str = "v2", kind: str = "paper",
            created_at: float | None = None, thesis: str = "", invalidation: str = "",
            horizon_days: int | None = None, hedge_symbol: str | None = None, benchmark: str | None = None,
            fuse_id: int | None = None, tags: list[str] | None = None) -> int:
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")
    if not symbol or not symbol.strip():
        raise ValueError("symbol is required")
    with conn:
        cur = conn.execute(
            "INSERT INTO bets(created_at, source, kind, fuse_id, symbol, hedge_symbol, asset_class, side, thesis,"
            " invalidation, horizon_days, benchmark, tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (created_at if created_at is not None else time.time(), source, kind, fuse_id, symbol.strip(),
             hedge_symbol, asset_class(symbol), side, thesis, invalidation, horizon_days, benchmark,
             db.jl(tags or [])))
    return cur.lastrowid


def _leg_return(bars: list[Bar], ts: float, h: int | None) -> tuple[float, float, float, float] | None:
    """(return, entry_ts, entry_px, exit_ts) from entry to h sessions later, or None if not there yet."""
    i = entry_index(bars, ts)
    if i is None:
        return None
    j = len(bars) - 1 if h is None else i + h
    if j >= len(bars) or j <= i:
        return None
    return bars[j][1] / bars[i][1] - 1, bars[i][0], bars[i][1], bars[j][0]


def _bench_return(bars: list[Bar], t0: float, t1: float) -> float | None:
    a, b = price_on_or_before(bars, t0), price_on_or_before(bars, t1)
    return None if not a or not b else b / a - 1


def mark_bet(conn: sqlite3.Connection, bet: sqlite3.Row, provider: PriceProvider, now: float) -> int:
    """Recompute every mark that is available for one bet. Returns how many were written."""
    bars = complete(provider.bars(bet["symbol"]), now)
    hedge = complete(provider.bars(bet["hedge_symbol"]), now) if bet["hedge_symbol"] else None
    if not bars or (bet["hedge_symbol"] and not hedge):
        return 0
    spy = complete(provider.bars(MARKET), now)
    bench = complete(provider.bars(bet["benchmark"]), now) if bet["benchmark"] else []
    sign = 1 if bet["side"] == "LONG" else -1
    written = 0
    for name, h in HORIZONS:
        leg = _leg_return(bars, bet["created_at"], h)
        if leg is None:
            continue
        raw, t0, px0, t1 = leg
        if hedge is not None:
            other = _leg_return(hedge, bet["created_at"], h)
            if other is None:
                continue
            raw -= other[0]
        ret = sign * raw
        m = _bench_return(spy, t0, t1) if spy else None
        s = _bench_return(bench, t0, t1) if bench else None
        with conn:
            if bet["entry_px"] is None:
                conn.execute("UPDATE bets SET entry_at = ?, entry_px = ? WHERE id = ?", (t0, px0, bet["id"]))
            conn.execute(
                "INSERT OR REPLACE INTO marks(bet_id, horizon, ret, vs_spy, vs_bench, marked_at) VALUES (?,?,?,?,?,?)",
                (bet["id"], name, ret, None if m is None else ret - sign * m,
                 None if s is None else ret - sign * s, now))
        written += 1
    return written


def mark_all(conn: sqlite3.Connection, provider: PriceProvider, now: float | None = None) -> int:
    now = now or time.time()
    return sum(mark_bet(conn, b, provider, now) for b in conn.execute("SELECT * FROM bets").fetchall())


def _rows(conn: sqlite3.Connection, where: str = "", params: tuple = (), unique: bool = False) -> list[dict]:
    q = ("SELECT b.*, m.horizon, m.ret, m.vs_spy, m.vs_bench FROM bets b JOIN marks m ON m.bet_id = b.id "
         + (f"WHERE {where} " if where else "") + "ORDER BY b.created_at, b.id")
    rows = [dict(r) for r in conn.execute(q, params)]
    if unique:   # keep only the first time each (source, symbol, side) was issued
        first: dict[tuple, int] = {}
        for r in rows:
            first.setdefault((r["source"], r["symbol"], r["hedge_symbol"], r["side"]), r["id"])
        rows = [r for r in rows if first[(r["source"], r["symbol"], r["hedge_symbol"], r["side"])] == r["id"]]
    return rows


def _group_key(r: dict, by: str):
    if by == "tag":
        return json.loads(r["tags"]) or ["(untagged)"]
    return [r[by]]


def scoreboard(conn: sqlite3.Connection, by: str = "source", unique: bool = True,
               where: str = "", params: tuple = ()) -> list[dict]:
    """Per group and horizon: n, mean and median return, win rate, and how it did against the benchmarks."""
    if by not in ("source", "kind", "side", "asset_class", "symbol", "tag"):
        raise ValueError(f"cannot group by {by!r}")
    buckets: dict[tuple, list[dict]] = {}
    for r in _rows(conn, where, params, unique):
        for g in _group_key(r, by):
            buckets.setdefault((g, r["horizon"]), []).append(r)
    order = {h: i for i, (h, _) in enumerate(HORIZONS)}
    out = []
    for (g, h), rs in sorted(buckets.items(), key=lambda kv: (str(kv[0][0]), order[kv[0][1]])):
        rets = [r["ret"] for r in rs]
        spy = [r["vs_spy"] for r in rs if r["vs_spy"] is not None]
        bench = [r["vs_bench"] for r in rs if r["vs_bench"] is not None]
        out.append({
            "group": g, "horizon": h, "n": len(rs),
            "mean": statistics.mean(rets), "median": statistics.median(rets),
            "win": sum(x > 0 for x in rets) / len(rets),
            "vs_spy": statistics.mean(spy) if spy else None,
            "beat_spy": sum(x > 0 for x in spy) / len(spy) if spy else None,
            "vs_bench": statistics.mean(bench) if bench else None,
        })
    return out


def format_scoreboard(rows: list[dict]) -> str:
    pct = lambda v: "    n/a" if v is None else f"{v * 100:+6.1f}%"
    lines = [f"{'group':14}{'horizon':8}{'n':>4}  {'mean':>7} {'median':>7} {'win':>5}  {'vs SPY':>7} {'beat':>5}  {'vs sector':>9}"]
    for r in rows:
        beat = "  n/a" if r["beat_spy"] is None else f"{r['beat_spy'] * 100:4.0f}%"
        lines.append(f"{str(r['group'])[:13]:14}{r['horizon']:8}{r['n']:>4}  {pct(r['mean'])} {pct(r['median'])} "
                     f"{r['win'] * 100:4.0f}%  {pct(r['vs_spy'])} {beat}  {pct(r['vs_bench']):>9}")
    return "\n".join(lines)
