"""Bet placement: the one route from a model's proposal to the ledger and the broker.

Code checks that the instrument really trades, records the bet in the scored ledger,
and sends a sized order through the risk limits. A conviction of 0 is a shadow bet:
recorded and scored like any other, with no order. Shadow bets are free and are the
system's main learning signal. Closing a bet goes through here too: it stops the bet's
score at the exit and sells whatever the paper account holds for it.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from . import ledger
from .prices import PriceProvider, complete

DAY = 86400.0
STALE_QUOTE_DAYS = 10


class BetOut(BaseModel):
    symbol: str
    side: Literal["LONG", "SHORT"]
    thesis: str
    invalidation: str
    horizon_days: int
    benchmark: str = ""
    conviction: int = 3
    tags: list[str] = Field(default_factory=list)


def tradeable(provider: PriceProvider, symbol: str, now: float) -> bool:
    """A symbol counts only if it has a recent completed session. This is what catches defunct tickers."""
    bars = complete(provider.bars(symbol), now)
    return bool(bars) and now - bars[-1][0] <= STALE_QUOTE_DAYS * DAY


def place_bet(conn: sqlite3.Connection, provider: PriceProvider, broker, b: BetOut, fuse_id: int | None, now: float) -> dict:
    """Check the instrument, record the bet in the ledger, and send a sized order through the risk limits."""
    sym = b.symbol.strip().upper()
    if not sym or not tradeable(provider, sym, now):
        return {"symbol": sym, "why": "no recent price; not a tradeable instrument"}
    bench = b.benchmark.strip().upper()
    bench = bench if bench and bench != sym and tradeable(provider, bench, now) else None
    conviction = max(0, min(5, int(b.conviction)))
    bet_id = ledger.add_bet(conn, symbol=sym, side=b.side, source="v2", kind="paper" if conviction else "shadow", created_at=now,
                            thesis=b.thesis.strip()[:1500], invalidation=b.invalidation.strip()[:600],
                            horizon_days=max(5, min(365, int(b.horizon_days))), benchmark=bench, fuse_id=fuse_id,
                            tags=[t.strip().lower()[:30] for t in b.tags if t.strip()][:4])
    if conviction == 0:
        return {"bet": bet_id, "symbol": sym, "side": b.side, "conviction": 0, "qty": 0, "order": "none (shadow bet)"}
    qty = broker.shares_for(sym, broker.limits.max_position_usd * conviction / 5, now)
    _, ok = broker.submit(sym, "BUY" if b.side == "LONG" else "SELL", qty, bet_id=bet_id, now=now)
    return {"bet": bet_id, "symbol": sym, "side": b.side, "conviction": conviction, "qty": qty,
            "order": "placed" if ok else "blocked: " + "; ".join(ok.reasons)}


def close_position(conn: sqlite3.Connection, broker, bet_id: int, reason: str, now: float) -> dict | None:
    """Close an open bet: freeze its score at the exit and unwind any paper position it opened. None if not open."""
    bet = conn.execute("SELECT * FROM bets WHERE id = ? AND closed_at IS NULL", (bet_id,)).fetchone()
    if bet is None or not ledger.close_bet(conn, bet_id, reason, now):
        return None
    held = sum(o["qty"] if o["action"] == "BUY" else -o["qty"] for o in conn.execute(
        "SELECT action, qty FROM orders WHERE bet_id = ? AND status IN ('pending', 'filled')", (bet_id,)))
    order = "none (nothing held)"
    if abs(held) > 1e-9 and broker is not None:
        _, ok = broker.submit(bet["symbol"], "SELL" if held > 0 else "BUY", abs(held), bet_id=bet_id, now=now)
        order = "placed" if ok else "blocked: " + "; ".join(ok.reasons)
    return {"bet": bet_id, "symbol": bet["symbol"], "side": bet["side"], "order": order}


def close_expired(conn: sqlite3.Connection, broker, now: float, source: str = "v2") -> int:
    """Close every open bet whose stated horizon has passed. A bet said how long it was for; holding on is a new bet."""
    due = conn.execute("SELECT id FROM bets WHERE source = ? AND closed_at IS NULL AND horizon_days IS NOT NULL"
                       " AND created_at + horizon_days * ? <= ?", (source, DAY, now)).fetchall()
    return sum(close_position(conn, broker, r["id"], "horizon reached", now) is not None for r in due)
