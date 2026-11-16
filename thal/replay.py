"""Replay: run the analyst over a past period, one simulated day at a time, and score it at once.

The models know nothing after their training cutoff, and prices after it are already known. So a period
after the cutoff can be replayed in hours instead of lived through in months. Nothing from the future may
reach the session:
  events   are copied in from a source database only once the simulated clock has passed the time they
           were first seen
  prices   pass through AsOf, which hides every bar dated after the simulated clock
  the web  is off: no web search, no page fetching, no prediction markets (their quotes are today's)
  memory   starts empty (the shipped seed was written later) and is built forward by the replay itself
Each session runs after the US close of its day, so that day's closing prices are fair to use and a bet
is entered at the next session's close, by the same rule as live.

A replay keeps its place in its own folder, so it can be stopped and resumed, and it stops when it has
spent its budget. It never touches the live database or wiki.
"""
from __future__ import annotations

import calendar
import sqlite3
import time
from pathlib import Path

from . import agent, db, dream, forecasts, ledger, report, risk
from .broker import get_broker
from .llm import LLM, BudgetExceeded
from .prices import Bar, YahooProvider
from .wiki import Wiki

DAY = 86400.0
SESSION_AT = 21.5 * 3600        # UTC; after the US close in winter and summer
EVENT_COLS = ("id", "item_id", "at", "event_type", "summary", "entities", "commodities", "places", "magnitude", "novelty",
              "reliability", "model")
NOTE = ("This is a replay of {date}. You have only what was known by then: the event log, memory and prices up to "
        "today's close. There is no web access and there are no prediction-market contracts in this session, so "
        "every forecast is a price forecast.")


class AsOf:
    """A price provider that only shows bars dated at or before its clock. The inner provider fetches at real time."""

    def __init__(self, inner, now: float = 0.0):
        self.inner, self.now = inner, now

    def bars(self, symbol: str, now: float | None = None, max_age: float | None = None) -> list[Bar]:
        return [b for b in self.inner.bars(symbol) if b[0] <= self.now]


def day_start(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%d")))


def feed(conn: sqlite3.Connection, src: sqlite3.Connection, after: float, upto: float, seen: bool = False) -> int:
    """Copy the events first seen in (after, upto] from the source into the replay database. Returns how many."""
    rows = src.execute(f"SELECT i.source, i.url, i.title, i.body, i.published_at, i.fetched_at, "
                       + ", ".join(f"e.{c} AS {c}" for c in EVENT_COLS)
                       + " FROM events e JOIN items i ON i.id = e.item_id WHERE i.fetched_at > ? AND i.fetched_at <= ? ORDER BY e.id",
                       (after, upto)).fetchall()
    with conn:
        for r in rows:
            conn.execute("INSERT OR IGNORE INTO items(id, source, url, title, body, published_at, fetched_at, extracted, fetch_status)"
                         " VALUES (?,?,?,?,?,?,?,1,'replay')",
                         (r["item_id"], r["source"], r["url"], r["title"], r["body"], r["published_at"], r["fetched_at"]))
            conn.execute(f"INSERT OR IGNORE INTO events({', '.join(EVENT_COLS)}, connected) VALUES ({', '.join('?' * len(EVENT_COLS))}, ?)",
                         tuple(r[c] for c in EVENT_COLS) + (int(seen),))
    return len(rows)


def session_days(src: sqlite3.Connection, start: float, end: float, every: int = 1) -> list[float]:
    """Midnights of the weekdays to hold a session on: those in [start, end] on which the source saw something new
    since the previous session day, thinned to every Nth. Weekends wait for Monday, when prices exist again."""
    days, d, last = [], start, start - DAY
    while d <= end:
        if time.gmtime(d).tm_wday < 5:
            new = src.execute("SELECT 1 FROM items WHERE fetched_at > ? AND fetched_at <= ? LIMIT 1",
                              (last + SESSION_AT, d + SESSION_AT)).fetchone()
            if new:
                days.append(d)
                last = d
        d += DAY
    return days[::max(1, every)]


def _kv(conn, k, default=None):
    r = conn.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    return r["v"] if r else default


def _set(conn, k, v) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (k, str(v)))


def spent(conn: sqlite3.Connection) -> float:
    return conn.execute("SELECT COALESCE(SUM(cost), 0) FROM runs").fetchone()[0]


def run(conn: sqlite3.Connection, src: sqlite3.Connection, llm: LLM, wiki: Wiki, provider: AsOf, broker, *, start: float, end: float,
        budget: float, every: int = 1, dream_every: int = 1, reflect_every: int = 5, say=print) -> dict:
    """Hold sessions from start to end until the budget is spent. Resumes after the last finished session."""
    rep = {"sessions": 0, "dreams": 0, "stopped": ""}
    done_to = float(_kv(conn, "replay_done_to", 0))
    if not done_to:                                         # first run: everything before the start is history already read
        rep["history"] = feed(conn, src, 0, start - DAY + SESSION_AT, seen=True)
        done_to = start - DAY + SESSION_AT
        _set(conn, "replay_done_to", done_to)
    n = int(_kv(conn, "replay_sessions", 0))
    for d in session_days(src, start, end, every):
        now = d + SESSION_AT
        if now <= done_to:
            continue
        if spent(conn) >= budget:
            rep["stopped"] = f"budget of ${budget:.2f} reached"
            break
        provider.now = now
        new = feed(conn, src, done_to, now)
        dream.housekeeping(conn, provider, broker, now)     # expire fuses and bets, fill orders, mark, settle forecasts
        date = time.strftime("%Y-%m-%d", time.gmtime(now))
        try:
            a = agent.run_session(conn, llm, wiki, provider, broker, now, offline=NOTE.format(date=date))
            if a is not None and not a["finished"]:
                say(f"{date}: session did not finish; its events will be read again")
            n += 1
            rep["sessions"] += 1
            line = (f"{date}: {new} new events, " + ("nothing to read" if a is None else
                    f"{a['forecasts']} forecasts, {len(a['bets'])} bets, {a['bets_closed']} closed, {a['fuses_opened']} fuses, "
                    f"{a['turns']} turns, {a['tool_errors']} tool errors"))
            if a is not None and n % max(1, dream_every) == 0:
                s = dream.dream_session(conn, llm, wiki, now)
                rep["dreams"] += 1
                line += f"; dream wrote {len(s['pages_written'])} pages" + ("" if s["finished"] else " (unfinished)")
                if (n // max(1, dream_every)) % max(1, reflect_every) == 0 and dream.reflect(conn, llm, wiki, now):
                    line += ", lessons rewritten"
        except BudgetExceeded as e:                         # the per-day cap inside the model client
            say(f"{date}: {e}")
            line = f"{date}: stopped by the daily cap"
        done_to = now
        _set(conn, "replay_done_to", done_to)
        _set(conn, "replay_sessions", n)
        say(line + f". Spent ${spent(conn):.2f} so far.")
    rep["spent"] = round(spent(conn), 2)
    return rep


def score(conn: sqlite3.Connection, provider: AsOf, broker, now: float | None = None) -> str:
    """Score everything recorded so far against prices up to now (real time by default)."""
    now = now or time.time()
    provider.now = now
    dream.housekeeping(conn, provider, broker, now)
    lines = [report.scorecard(conn, now), "", "Bets by horizon", ledger.format_scoreboard(ledger.scoreboard(conn, by="source", unique=False,
                                                                                                         where="b.source = 'v2'"))]
    tags = sorted(forecasts.scores(conn, "tag"), key=lambda r: -r["n"])[:12]
    if tags:
        lines += ["", "Forecasts by tag"] + [f"- {r['group']}: n={r['n']}, skill {r['skill']:+.0%}" for r in tags]
    return "\n".join(lines)


def open_replay(folder: str | Path, source: str | Path):
    """The replay's own database, wiki, clock-bound prices and paper broker, plus the read-only source of events."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    conn = db.connect(folder / "thal.db")
    src = sqlite3.connect(f"file:{Path(source)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    provider = AsOf(YahooProvider(conn))
    return conn, src, LLM(conn), Wiki(folder / "wiki"), provider, get_broker(conn, provider, risk.load())
