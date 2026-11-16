import sqlite3
import time
import pytest
from thal import agent, db, forecasts as fc, replay
from thal.broker import PaperBroker
from thal.llm import LLM
from thal.prices import FakeProvider, complete, latest
from thal.risk import Limits
from thal.wiki import Wiki
from .conftest import DAY, T0, series
from .test_agent import Script, resp, text, use

D0 = T0 - T0 % DAY                      # midnight of day 0; the price series opens at 14:30 that day
START = D0 + 70 * DAY                   # the replay starts on day 70


def source(tmp_path, rows):
    """A source database: rows of (item id, first seen, summary)."""
    c = db.connect(tmp_path / "src.db")
    for i, (seen, summary) in enumerate(rows, 1):
        c.execute("INSERT INTO items(id, source, title, fetched_at) VALUES (?, 'v1: wire', ?, ?)", (f"i{i}", summary, seen))
        c.execute("INSERT INTO events(id, item_id, at, event_type, summary, magnitude, novelty) VALUES (?, ?, ?, 'other', ?, 3, 3)",
                  (i, f"i{i}", seen - 3600, summary))
    c.commit()
    c.row_factory = sqlite3.Row
    return c


def weekday(d):
    while time.gmtime(d).tm_wday >= 5:
        d += DAY
    return d


@pytest.fixture
def rig(conn, tmp_path):
    prov = replay.AsOf(FakeProvider({"SPY": series(100, 0.0, 120), "CANE": series(20, 0.01, 120)}))
    broker = PaperBroker(conn, prov, Limits(kill_switch="/nonexistent/KILL"), starting_cash=100_000)
    return conn, Wiki(tmp_path / "wiki"), prov, broker


def test_asof_hides_every_bar_after_its_clock():
    prov = replay.AsOf(FakeProvider({"CANE": series(20, 0.01, 120)}), now=T0 + 10 * DAY + 7 * 3600)   # 21:30 on day 10
    bars = prov.bars("CANE")
    assert len(bars) == 11 and bars[-1][0] == T0 + 10 * DAY
    assert latest(prov, "CANE", prov.now)[-1][0] == T0 + 10 * DAY           # today's close is known after the bell
    assert complete(bars, prov.now)[-1][0] == T0 + 9 * DAY                  # but scoring still waits for a finished session
    prov.now += DAY
    assert len(prov.bars("CANE")) == 12


def test_feed_brings_in_only_what_had_been_seen_and_marks_history_as_read(conn, tmp_path):
    src = source(tmp_path, [(START - 5 * DAY, "old news"), (START + 1000, "today"), (START + 3 * DAY, "the future")])
    assert replay.feed(conn, src, 0, START, seen=True) == 1
    assert replay.feed(conn, src, START, START + DAY) == 1
    assert [(r["summary"], r["connected"]) for r in conn.execute("SELECT * FROM events ORDER BY id")] == [("old news", 1), ("today", 0)]
    assert replay.feed(conn, src, START, START + DAY) == 1 and conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2   # safe to repeat
    assert conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'future'").fetchone()[0] == 0


def test_session_days_are_weekdays_with_news_thinned_to_every_nth(tmp_path):
    mon = weekday(START)
    while time.gmtime(mon).tm_wday != 0:
        mon += DAY
    src = source(tmp_path, [(mon + 3600, "a"), (mon + DAY + 3600, "b"), (mon + 5 * DAY + 3600, "saturday"), (mon + 8 * DAY + 3600, "c")])
    days = replay.session_days(src, mon, mon + 10 * DAY)
    assert days == [mon, mon + DAY, mon + 7 * DAY, mon + 8 * DAY]           # Saturday's news is read on the Monday
    assert replay.session_days(src, mon, mon + 10 * DAY, every=2) == [mon, mon + 7 * DAY]


def test_replay_session_is_offline_dated_and_cannot_see_later_prices_or_events(rig, tmp_path):
    conn, wiki, prov, broker = rig
    d = weekday(START)
    src = source(tmp_path, [(d - 3 * DAY, "history"), (d + 3600, "drought in the cane belt"), (d + 9 * DAY, "the future")])
    client = Script(
        resp("tool_use", use(1, "get_prices", symbol="CANE"), use(2, "fetch_url", url="https://example.com"),
             use(3, "search_events", query="future history"),
             use(4, "forecast_price", symbol="CANE", direction="above", level=45, days=5, probability=0.6),
             use(5, "propose_bet", symbol="CANE", side="LONG", thesis="t", invalidation="i", horizon_days=20, conviction=0)),
        resp("end_turn", text("done")))
    llm = LLM(conn, client=client)
    rep = replay.run(conn, src, llm, wiki, prov, broker, start=d, end=d, budget=50, dream_every=99, say=lambda s: None)
    assert rep["sessions"] == 1 and rep["history"] == 1
    first = client.calls[0]
    names = {t["name"] for t in first["tools"]}
    assert not names & {"fetch_url", "find_markets", "forecast_market", "web_search"} and "forecast_price" in names
    opening = first["messages"][0]["content"]
    assert opening.startswith(f"Today is {time.strftime('%Y-%m-%d', time.gmtime(d))}. This is a replay") and "drought" in opening
    assert "the future" not in opening and "history" not in opening          # history counts as already read
    results = {r["tool_use_id"]: r for r in client.calls[1]["messages"][-1]["content"]}
    day = int((d - D0) / DAY)
    assert f"{20 * 1.01 ** day:.2f}" in results["tu1"]["content"] and f"{20 * 1.01 ** (day + 1):.2f}" not in results["tu1"]["content"]
    assert results["tu2"].get("is_error") and "unknown tool" in results["tu2"]["content"]
    assert "history" in results["tu3"]["content"] and "the future" not in results["tu3"]["content"]
    f = conn.execute("SELECT * FROM forecasts").fetchone()
    assert f"last price {20 * 1.01 ** day:.2f}" in f["question"] and f["created_at"] == d + replay.SESSION_AT
    assert conn.execute("SELECT created_at FROM bets").fetchone()[0] == d + replay.SESSION_AT


def test_replay_scores_resumes_and_stops_at_its_budget(rig, tmp_path):
    conn, wiki, prov, broker = rig
    d1 = weekday(START); d2 = weekday(d1 + DAY); d3 = weekday(d2 + DAY)
    src = source(tmp_path, [(d1 + 3600, "one"), (d2 + 3600, "two"), (d3 + 3600, "three")])
    bet = resp("tool_use", use(1, "propose_bet", symbol="CANE", side="LONG", thesis="t", invalidation="i", horizon_days=20, conviction=0),
               use(2, "forecast_price", symbol="CANE", direction="above", level=20, days=3, probability=0.9))
    client = Script(bet, resp("end_turn", text("done")), resp("end_turn", text("nothing today")))
    llm = LLM(conn, client=client)
    cost_of_three_turns = 3 * llm.cost("claude-fable-5-2", resp("end_turn").usage)
    rep = replay.run(conn, src, llm, wiki, prov, broker, start=d1, end=d3, budget=cost_of_three_turns, dream_every=99, say=lambda s: None)
    assert rep["sessions"] == 2 and "budget" in rep["stopped"]              # the third day never started
    client.turns.append(resp("end_turn", text("day three")))
    rep = replay.run(conn, src, llm, wiki, prov, broker, start=d1, end=d3, budget=99, dream_every=99, say=lambda s: None)
    assert rep["sessions"] == 1 and "history" not in rep and not client.turns   # resumed at day three only
    out = replay.score(conn, prov, broker, now=d3 + 40 * DAY)
    entry = int((d1 - D0) / DAY) + 1                                        # entered at the next session's close
    b = conn.execute("SELECT * FROM bets").fetchone()
    assert b["entry_px"] == pytest.approx(20 * 1.01 ** entry) and b["closed_at"] is not None     # its 20-day horizon ran out
    assert conn.execute("SELECT outcome FROM forecasts").fetchone()[0] == 1 and "1 resolved" in out and "Bets by horizon" in out


def test_a_live_session_keeps_its_web_tools_and_is_told_the_date(rig):
    conn, wiki, prov, broker = rig
    prov.now = START + 20 * 3600
    conn.execute("INSERT INTO items(id, source, title, fetched_at) VALUES ('i','S','t',1)")
    conn.execute("INSERT INTO events(item_id, at, event_type, summary) VALUES ('i', ?, 'other', 'something')", (START,))
    client = Script(resp("end_turn", text("done")))
    agent.run_session(conn, LLM(conn, client=client), wiki, prov, broker, prov.now)
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert {"fetch_url", "find_markets", "forecast_market", "web_search"} <= names
    assert client.calls[0]["messages"][0]["content"].startswith("Today is ") and "replay" not in client.calls[0]["messages"][0]["content"]
