"""Daily price bars, with one timing rule used everywhere.

A bar is (session_open_ts, adjusted_close). An idea issued at time T is entered at the
close of the first *complete* session that opened after T. That is at most one session
late and never looks ahead; the ledger and the paper broker both use it.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Protocol

Bar = tuple[float, float]
SESSION_SECONDS = 20 * 3600   # a bar is trusted as final 20h after its session opened
STALE_SECONDS = 6 * 3600


class PriceProvider(Protocol):
    def bars(self, symbol: str) -> list[Bar]: ...


class FakeProvider:
    """In-memory prices for tests."""

    def __init__(self, data: dict[str, list[Bar]] | None = None):
        self.data = data or {}

    def bars(self, symbol: str) -> list[Bar]:
        return list(self.data.get(symbol, []))


class YahooProvider:
    """Yahoo daily adjusted closes, cached in the bars table."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d&events=div"

    def __init__(self, conn: sqlite3.Connection, start_ts: float = 1767225600.0, fetch=None):
        self.conn = conn
        self.start_ts = start_ts          # 2026-01-01
        self._fetch = fetch or self._http

    def _http(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def refresh(self, symbol: str, now: float | None = None) -> bool:
        now = now or time.time()
        url = self.URL.format(sym=urllib.parse.quote(symbol), p1=int(self.start_ts), p2=int(now) + 86400)
        try:
            res = self._fetch(url)["chart"]["result"][0]
            rows = [(symbol, float(t), float(c))
                    for t, c in zip(res["timestamp"], res["indicators"]["adjclose"][0]["adjclose"])
                    if c is not None]
            ok = bool(rows)
        except Exception:
            rows, ok = [], False
        with self.conn:
            if ok:   # adjusted closes are restated on dividends, so replace the whole series
                self.conn.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
                self.conn.executemany("INSERT INTO bars(symbol, ts, close) VALUES (?,?,?)", rows)
            self.conn.execute("INSERT OR REPLACE INTO bars_meta(symbol, fetched_at, ok) VALUES (?,?,?)",
                              (symbol, now, int(ok)))
        return ok

    def bars(self, symbol: str, now: float | None = None) -> list[Bar]:
        now = now or time.time()
        meta = self.conn.execute("SELECT fetched_at FROM bars_meta WHERE symbol = ?", (symbol,)).fetchone()
        if meta is None or now - meta["fetched_at"] > STALE_SECONDS:
            self.refresh(symbol, now)
        return [(r["ts"], r["close"]) for r in
                self.conn.execute("SELECT ts, close FROM bars WHERE symbol = ? ORDER BY ts", (symbol,))]


def complete(bars: list[Bar], now: float) -> list[Bar]:
    """Drop the bar of a session that may still be trading."""
    return [b for b in bars if now - b[0] >= SESSION_SECONDS]


def entry_index(bars: list[Bar], ts: float) -> int | None:
    """Index of the first bar whose session opened after ts."""
    for i, (t, _) in enumerate(bars):
        if t > ts:
            return i
    return None


def price_on_or_before(bars: list[Bar], ts: float) -> float | None:
    """Last close of a session that opened at or before ts (compared by calendar day)."""
    day = int(ts // 86400)
    best = None
    for t, c in bars:
        if int(t // 86400) <= day:
            best = c
        else:
            break
    return best
