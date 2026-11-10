"""Brokers. PaperBroker simulates fills from daily bars with the ledger's timing rule.

An order placed at time T fills at the close of the first complete session that opened
after T. Pending orders count toward the risk limits, so a burst of orders cannot
slip past them before any has filled.
"""
from __future__ import annotations

import math
import sqlite3
import time

from . import risk
from .ledger import asset_class
from .prices import PriceProvider, complete, entry_index

DAY = 86400.0


class PaperBroker:
    mode = "paper"

    def __init__(self, conn: sqlite3.Connection, provider: PriceProvider, limits: risk.Limits,
                 starting_cash: float = 100_000.0):
        if limits.mode != "paper":
            raise ValueError("PaperBroker only runs in paper mode")
        self.conn, self.provider, self.limits = conn, provider, limits
        row = conn.execute("SELECT v FROM kv WHERE k = 'paper_cash'").fetchone()
        if row is None:
            with conn:
                conn.execute("INSERT INTO kv(k, v) VALUES ('paper_cash', ?)", (str(float(starting_cash)),))
            self.starting_cash = float(starting_cash)
        else:
            self.starting_cash = float(row["v"])

    def _last_price(self, symbol: str, now: float) -> float | None:
        bars = complete(self.provider.bars(symbol), now)
        return bars[-1][1] if bars else None

    def portfolio(self, now: float | None = None, include_pending: bool = False) -> risk.Portfolio:
        now = now or time.time()
        cash, pos = self.starting_cash, {}
        statuses = ("filled", "pending") if include_pending else ("filled",)
        q = f"SELECT * FROM orders WHERE mode = 'paper' AND status IN ({','.join('?' * len(statuses))}) ORDER BY id"
        for o in self.conn.execute(q, statuses):
            px = o["fill_px"] if o["status"] == "filled" else self._last_price(o["symbol"], now)
            if px is None:
                continue
            signed = o["qty"] if o["action"] == "BUY" else -o["qty"]
            pos[o["symbol"]] = pos.get(o["symbol"], 0.0) + signed
            cash -= signed * px
        pos = {s: q for s, q in pos.items() if abs(q) > 1e-9}
        prices = {s: self._last_price(s, now) or 0.0 for s in pos}
        return risk.Portfolio(cash=cash, positions=pos, prices=prices)

    def submit(self, symbol: str, action: str, qty: float, bet_id: int | None = None,
               now: float | None = None) -> tuple[int, risk.Decision]:
        """Risk-check and record an order. Returns (order id, decision)."""
        now = now or time.time()
        px = self._last_price(symbol, now)
        today = self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE mode = 'paper' AND status != 'rejected' AND at >= ?",
            (now - (now % DAY),)).fetchone()[0]
        req = risk.OrderRequest(symbol, action, qty, px if px else float("nan"), asset_class(symbol))
        decision = risk.check(req, self.portfolio(now, include_pending=True), self.limits, today)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders(at, bet_id, mode, symbol, action, qty, status, reason) VALUES (?,?,?,?,?,?,?,?)",
                (now, bet_id, "paper", symbol, action, qty if isinstance(qty, (int, float)) and math.isfinite(qty) else 0,
                 "pending" if decision else "rejected", "; ".join(decision.reasons)))
        return cur.lastrowid, decision

    def settle(self, now: float | None = None) -> int:
        """Fill pending orders whose fill session has completed. Returns the number filled."""
        now = now or time.time()
        filled = 0
        for o in self.conn.execute("SELECT * FROM orders WHERE mode = 'paper' AND status = 'pending'").fetchall():
            bars = complete(self.provider.bars(o["symbol"]), now)
            i = entry_index(bars, o["at"])
            if i is None:
                continue
            with self.conn:
                self.conn.execute("UPDATE orders SET status = 'filled', fill_at = ?, fill_px = ? WHERE id = ?",
                                  (bars[i][0], bars[i][1], o["id"]))
            filled += 1
        return filled

    def shares_for(self, symbol: str, usd: float, now: float | None = None) -> int:
        px = self._last_price(symbol, now or time.time())
        return int(usd // px) if px and px > 0 else 0


def get_broker(conn: sqlite3.Connection, provider: PriceProvider, limits: risk.Limits):
    if limits.mode == "paper":
        return PaperBroker(conn, provider, limits)
    raise NotImplementedError("live trading is not wired up yet; the IBKR adapter comes after the paper period")
