"""Forecasts: dated yes/no predictions that resolve in days, so the system learns fast.

"SYMBOL closes above/below LEVEL on DATE", on an instrument the system can trade. Scored by Brier
score against a no-view random walk using the symbol's own recent volatility, and resolved from
price bars. (A few early forecasts were on Kalshi prediction-market contracts; the system no longer
makes those, and the ones already recorded are still settled from the exchange's published result.)
Beating the baseline means the forecaster knew something the baseline did not. Matching it means nothing.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import urllib.request

from . import db
from .prices import PriceProvider, complete

DAY = 86400.0
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = "Mozilla/5.0 (compatible; thalamus-research/0.1; automated market-data reader)"
MAX_DAYS = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,                 -- price | market
    ref TEXT NOT NULL,                  -- symbol or market ticker
    question TEXT NOT NULL,
    direction TEXT, level REAL,         -- price forecasts only
    due_at REAL NOT NULL,
    prob REAL NOT NULL,                 -- the forecaster's probability of YES
    baseline REAL NOT NULL,             -- the no-view probability of YES at the time
    rationale TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    fuse_id INTEGER,
    resolved_at REAL, outcome INTEGER   -- 1 yes, 0 no
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA + "DROP TABLE IF EXISTS markets_fts; DROP TABLE IF EXISTS markets;")   # the old Kalshi catalogue


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def price_baseline(closes: list[float], level: float, direction: str, days: float) -> float:
    """P(close above/below level after `days` calendar days) for a driftless random walk with the recent daily volatility."""
    rets = [math.log(b / a) for a, b in zip(closes[-61:], closes[-60:]) if a > 0 and b > 0]
    if len(rets) < 10:
        raise ValueError("not enough price history to form a baseline")
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) or 1e-6
    sessions = max(1.0, days * 5 / 7)
    above = 1 - _phi(math.log(level / closes[-1]) / (vol * math.sqrt(sessions)))
    p = above if direction == "above" else 1 - above
    return min(0.99, max(0.01, p))


def _check_prob(prob) -> float:
    if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not math.isfinite(prob) or not 0.01 <= prob <= 0.99:
        raise ValueError("probability must be between 0.01 and 0.99")
    return float(prob)


def add_price(conn: sqlite3.Connection, provider: PriceProvider, symbol: str, direction: str, level: float, days: int,
              prob: float, rationale: str = "", tags: list[str] | None = None, fuse_id: int | None = None,
              now: float | None = None) -> dict:
    ensure(conn)
    now = now or time.time()
    sym = symbol.strip().upper()
    if direction not in ("above", "below"):
        raise ValueError("direction must be above or below")
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) or level <= 0:
        raise ValueError("level must be a positive number")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be a whole number from 1 to {MAX_DAYS}")
    prob = _check_prob(prob)
    bars = complete(provider.bars(sym), now)
    if not bars or now - bars[-1][0] > 10 * DAY:
        raise ValueError(f"{sym} has no recent price; cannot forecast it")
    base = price_baseline([c for _, c in bars], float(level), direction, days)
    due = (now - now % DAY) + days * DAY
    q = f"{sym} closes {direction} {level:g} on {time.strftime('%Y-%m-%d', time.gmtime(due))} (last close {bars[-1][1]:.2f})"
    with conn:
        cur = conn.execute("INSERT INTO forecasts(created_at, kind, ref, question, direction, level, due_at, prob, baseline, rationale,"
                           " tags, fuse_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (now, "price", sym, q, direction, float(level), due, prob, base, rationale.strip()[:800],
                            db.jl([t.strip().lower()[:30] for t in (tags or []) if t.strip()][:4]), fuse_id))
    return {"id": cur.lastrowid, "question": q, "prob": prob, "baseline": round(base, 3)}


def _json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def resolve(conn: sqlite3.Connection, provider: PriceProvider, get=_json, now: float | None = None) -> int:
    """Settle every forecast whose answer is now known. Returns how many were settled."""
    ensure(conn)
    now = now or time.time()
    done = 0
    for f in conn.execute("SELECT * FROM forecasts WHERE outcome IS NULL AND due_at <= ?", (now,)).fetchall():
        outcome = None
        if f["kind"] == "price":
            bars = complete(provider.bars(f["ref"]), now)
            due_day = int(f["due_at"] // DAY)
            on_or_before = [c for t, c in bars if int(t // DAY) <= due_day]
            settled = any(int(t // DAY) >= due_day for t, _ in bars) or now - f["due_at"] > 4 * DAY
            if on_or_before and settled:      # the close on the due date, or the last one before it if that day had no session
                outcome = int(on_or_before[-1] > f["level"]) if f["direction"] == "above" else int(on_or_before[-1] < f["level"])
        else:
            try:
                result = (get(f"{KALSHI}/markets/{f['ref']}").get("market") or {}).get("result")
            except Exception:
                result = None
            outcome = {"yes": 1, "no": 0}.get(result)
        if outcome is not None:
            with conn:
                conn.execute("UPDATE forecasts SET outcome = ?, resolved_at = ? WHERE id = ?", (outcome, now, f["id"]))
            done += 1
    return done


def scores(conn: sqlite3.Connection, by: str | None = None) -> list[dict]:
    """Brier score of the forecasts against their baselines; skill above 0 means better than the baseline."""
    ensure(conn)
    groups: dict[str, list] = {}
    for f in conn.execute("SELECT * FROM forecasts WHERE outcome IS NOT NULL"):
        keys = [f["kind"]] if by == "kind" else (json.loads(f["tags"]) or ["(untagged)"]) if by == "tag" else ["all"]
        for k in keys:
            groups.setdefault(k, []).append(f)
    out = []
    for k, fs in sorted(groups.items()):
        brier = sum((f["prob"] - f["outcome"]) ** 2 for f in fs) / len(fs)
        base = sum((f["baseline"] - f["outcome"]) ** 2 for f in fs) / len(fs)
        out.append({"group": k, "n": len(fs), "brier": brier, "baseline_brier": base,
                    "skill": 1 - brier / base if base > 0 else 0.0,
                    "hit": sum((f["prob"] > 0.5) == bool(f["outcome"]) for f in fs) / len(fs)})
    return out


def scores_text(conn: sqlite3.Connection) -> str:
    rows = scores(conn) + scores(conn, "kind")
    open_n = conn.execute("SELECT COUNT(*) FROM forecasts WHERE outcome IS NULL").fetchone()[0]
    if not rows:
        return f"no forecasts resolved yet ({open_n} open)"
    return "\n".join(f"- {r['group']}: n={r['n']}, Brier {r['brier']:.3f} vs baseline {r['baseline_brier']:.3f} "
                     f"(skill {r['skill']:+.0%}), right side {r['hit']:.0%}" for r in rows) + f"\n- {open_n} still open"
