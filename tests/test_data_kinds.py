"""Rainfall and prediction-market prices as data: dated addresses, amounts that add up, a long record read once,
daily contract prices kept as readings, sharp moves as items, and the analyst's tool for reading a series."""
import calendar
import copy
import json
from thal import forecasts as fc, ingest
from thal.agent import Session
from thal.prices import FakeProvider
from thal.wiki import Wiki

DAY = 86400.0
NOW = float(calendar.timegm((2026, 11, 17, 6, 0, 0)))
RAIN = {"name": "Rain at Testville", "kind": "series", "url": "https://w.example/d?sdate={days_ago:45}&edate={today}",
        "recipe": {"format": "json", "rows": "data", "time": "0", "value": "1", "unit": "inches", "min": 0, "sum": True, "map": {"T": 0},
                   "history": "https://w.example/d?sdate=2000-01-01&edate={today}"}}


def test_an_address_can_ask_for_a_window_of_dates():
    assert ingest.expand("https://x/d?s={days_ago:45}&e={today}&k={other}", NOW) == "https://x/d?s=2026-10-03&e=2026-11-17&k={other}"
    assert ingest.expand("https://x/plain", NOW) == "https://x/plain"


def test_rainfall_is_summed_compared_with_earlier_years_and_its_long_record_is_read_once(conn):
    recent = json.dumps({"data": [["2026-11-13", "0.50"], ["2026-11-14", "T"], ["2026-11-15", "1.00"], ["2026-11-16", "0.25"], ["2026-11-17", "M"]]})
    # the long record: every day of the same 30 days in six earlier years, at 0.01 inches a day times the year's rank
    past = json.dumps({"data": [[f"{2020 + y}-{m:02d}-{d:02d}", f"{0.01 * (y + 1):.2f}"] for y in range(6)
                                for m, days in ((10, range(18, 32)), (11, range(1, 17))) for d in days]})
    asked = []
    get = lambda url: asked.append(url) or (past if "2000-01-01" in url else recent)
    assert ingest.ingest(conn, [RAIN], get=get, now=NOW) == {"Rain at Testville": 1}
    assert asked == ["https://w.example/d?sdate=2000-01-01&edate=2026-11-17", "https://w.example/d?sdate=2026-10-03&edate=2026-11-17"]
    got = dict(conn.execute("SELECT day, value FROM readings WHERE day >= '2026-11-13'").fetchall())
    assert got == {"2026-11-13": 0.5, "2026-11-14": 0.0, "2026-11-15": 1.0, "2026-11-16": 0.25}          # a trace is zero, missing is skipped
    item = conn.execute("SELECT * FROM items").fetchone()
    assert item["title"] == "Rain at Testville: 0.25 inches on 2026-11-16; 1.75 inches in 7 days, 1.75 in 30 days, 1.75 this month so far"
    assert "higher than in 5 of the 6 earlier years on record for the same 30 days (those ranged from 0.3 to 1.8, median 1.05)" in item["body"]
    assert item["url"] == ""                                               # a data address is not an article to fetch
    asked.clear()
    ingest.ingest(conn, [RAIN], get=get, now=NOW + DAY)
    assert len(asked) == 1 and "2000-01-01" not in asked[0]                # the long record is not asked for again


def test_a_failing_history_address_does_not_stop_the_daily_read(conn):
    def get(url):
        if "2000-01-01" in url: raise OSError("down")
        return json.dumps({"data": [["2026-11-16", "0.25"]]})
    assert ingest.ingest(conn, [RAIN], get=get, now=NOW) == {"Rain at Testville": 1}


def market(ticker, bid, ask, volume="5000", close="2026-12-16T19:00:00Z"):
    return {"ticker": ticker, "yes_sub_title": ticker, "rules_primary": f"Rule for {ticker}.", "close_time": close,
            "yes_bid_dollars": bid, "yes_ask_dollars": ask, "volume_fp": volume, "status": "active", "result": ""}


EVENTS = {"events": [
    {"event_ticker": "KXRAIN", "category": "Climate and Weather", "title": "Rain in Testville in November",
     "markets": [market("KXRAIN-4", "0.30", "0.34"), market("KXRAIN-6", "0.10", "0.12"), market("KXRAIN-THIN", "0.30", "0.34", volume="200")]},
    {"event_ticker": "KXSHOW", "category": "Entertainment", "title": "Award", "markets": [market("KXSHOW-A", "0.30", "0.34")]}], "cursor": ""}
FEED = {"name": fc.MOVES_SOURCE, "kind": "kalshi", "url": "https://api.example/events"}


def test_market_prices_are_kept_daily_and_a_sharp_move_becomes_one_item(conn):
    fc.refresh_markets(conn, get=lambda u: EVENTS, now=NOW)
    kept = dict(conn.execute("SELECT source, value FROM readings").fetchall())
    assert kept == {"kalshi:KXRAIN-4": 0.32, "kalshi:KXRAIN-6": 0.11, "kalshi:KXRAIN-THIN": 0.32}        # not the entertainment contract
    assert ingest.ingest(conn, [FEED], now=NOW) == {fc.MOVES_SOURCE: 0}                                  # one day on record: nothing to compare
    later = copy.deepcopy(EVENTS)
    for m in later["events"][0]["markets"]:
        if m["ticker"] != "KXRAIN-6":
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.60", "0.64"
    fc.refresh_markets(conn, get=lambda u: later, now=NOW + DAY)
    assert ingest.ingest(conn, [FEED], now=NOW + DAY) == {fc.MOVES_SOURCE: 1}                            # the thinly traded one is left out
    item = conn.execute("SELECT * FROM items").fetchone()
    assert item["title"] == "Prediction market moved: Rain in Testville in November — KXRAIN-4: 32% to 62%"
    assert "A market price, not a report" in item["body"] and "Rule for KXRAIN-4." in item["body"] and item["url"] == ""
    assert ingest.ingest(conn, [FEED], now=NOW + DAY + 3600) == {fc.MOVES_SOURCE: 0}                     # once per contract and day
    assert conn.execute("SELECT COUNT(*) FROM readings WHERE source = 'kalshi:KXRAIN-4'").fetchone()[0] == 2
    assert fc.moves(conn, NOW + 5 * DAY) == []                                                           # no price today: nothing


def test_the_analyst_can_read_a_series_but_not_past_its_own_day(conn, tmp_path):
    for day, v in (("2026-11-15", 80.0), ("2026-11-16", 74.0), ("2026-11-18", 60.0)):
        conn.execute("INSERT INTO readings VALUES ('Rhine level at Kaub', ?, 1, ?, 'cm')", (day, v))
    conn.execute("INSERT INTO readings VALUES ('kalshi:KXRAIN-4', '2026-11-16', 1, 0.32, 'prob')")
    conn.commit()
    s = Session(conn, Wiki(tmp_path / "wiki"), FakeProvider({}), None, NOW)
    assert s.get_readings() == "Rhine level at Kaub (cm): 2 days, 2026-11-15 to 2026-11-16"              # market prices are listed only when asked for
    out = s.get_readings("rhine")
    assert "2 days on record from 2026-11-15, range 74 to 80" in out and out.endswith("11-15 80, 11-16 74")   # the 18th has not happened yet
    assert "kalshi:KXRAIN-4 (prob)" in s.get_readings("kalshi:KXRAIN") and "no series" in s.get_readings("volga")
    assert s.call("get_readings", {"name": "rhine", "days": 1})[0].endswith("11-16 74")
