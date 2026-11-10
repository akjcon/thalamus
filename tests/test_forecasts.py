import math
import pytest
from thal import forecasts as fc
from thal.prices import FakeProvider
from .conftest import DAY, T0, series

NOW = T0 + 70 * DAY + 3600


def wobble(px=100.0, n=71, amp=0.01):
    return [(T0 + i * DAY, px * (1 + amp * (1 if i % 2 else -1))) for i in range(n)]


@pytest.fixture
def prov():
    return FakeProvider({"AAA": wobble(), "DEAD": wobble(n=20)})


def test_baseline_is_a_coin_flip_at_the_money_and_falls_with_distance():
    closes = [c for _, c in wobble()]
    at = fc.price_baseline(closes, closes[-1], "above", 7)
    assert at == pytest.approx(0.5, abs=1e-9)
    far, farther = fc.price_baseline(closes, closes[-1] * 1.05, "above", 7), fc.price_baseline(closes, closes[-1] * 1.10, "above", 7)
    assert 0.01 <= farther < far < 0.5
    assert fc.price_baseline(closes, closes[-1] * 1.05, "below", 7) == pytest.approx(1 - far)
    assert fc.price_baseline(closes, closes[-1] * 1.05, "above", 28) > far                 # more time, more chance
    with pytest.raises(ValueError): fc.price_baseline(closes[:5], 100, "above", 7)


def test_price_forecast_is_recorded_with_its_baseline(conn, prov):
    r = fc.add_price(conn, prov, " aaa ", "above", 103, 7, 0.7, "LNG squeeze", ["Chokepoint "], now=NOW)
    assert r["id"] == 1 and "AAA closes above 103 on" in r["question"] and 0.01 <= r["baseline"] < 0.5
    f = conn.execute("SELECT * FROM forecasts").fetchone()
    assert f["due_at"] == (NOW - NOW % DAY) + 7 * DAY and f["tags"] == '["chokepoint"]' and f["outcome"] is None


@pytest.mark.parametrize("kw", [dict(direction="sideways"), dict(level=0), dict(level=float("nan")), dict(days=0), dict(days=400), dict(days=2.5),
                                dict(prob=0), dict(prob=1), dict(prob=1.2), dict(prob=True), dict(symbol="DEAD"), dict(symbol="NOPE")])
def test_bad_price_forecasts_are_rejected(conn, prov, kw):
    args = {**dict(symbol="AAA", direction="above", level=103, days=7, prob=0.6), **kw}
    with pytest.raises(ValueError): fc.add_price(conn, prov, now=NOW, **args)
    assert conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 0


def test_price_forecast_resolves_only_once_the_due_session_is_complete(conn):
    bars = wobble(n=71)
    prov = FakeProvider({"AAA": bars})
    fc.add_price(conn, prov, "AAA", "above", 100.5, 3, 0.8, now=NOW)       # due on day 73
    fc.add_price(conn, prov, "AAA", "below", 100.5, 3, 0.8, now=NOW)
    assert fc.resolve(conn, prov, now=NOW + 2 * DAY) == 0                   # not due yet
    prov.data["AAA"] = wobble(n=74)                                         # bar 73 exists but its session may be open
    assert fc.resolve(conn, prov, now=T0 + 73 * DAY + 3600) == 0
    assert fc.resolve(conn, prov, now=T0 + 74 * DAY) == 2                   # bar 73 closes at 101 (odd index)
    assert [r["outcome"] for r in conn.execute("SELECT outcome FROM forecasts ORDER BY id")] == [1, 0]
    assert fc.resolve(conn, prov, now=T0 + 80 * DAY) == 0                   # settled once


def test_due_date_without_a_session_uses_the_last_close_before_it(conn):
    prov = FakeProvider({"AAA": wobble(n=71)})
    fc.add_price(conn, prov, "AAA", "above", 100.5, 2, 0.6, now=NOW)        # due day 72, but no bars ever arrive (holiday/halt)
    assert fc.resolve(conn, prov, now=T0 + 73 * DAY) == 0
    assert fc.resolve(conn, prov, now=T0 + 77 * DAY) == 1
    assert conn.execute("SELECT outcome FROM forecasts").fetchone()[0] == 0  # last close (bar 70, even) is 99


EVENTS = {"events": [
    {"event_ticker": "KXFED-26DEC", "category": "Economics", "title": "Fed decision in December",
     "markets": [{"ticker": "KXFED-26DEC-CUT25", "yes_sub_title": "Cut 25bps", "rules_primary": "Resolves Yes if the Fed cuts by 25bps.",
                  "close_time": "2026-12-16T19:00:00Z", "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6400", "last_price_dollars": "0.6100", "volume_fp": "5000", "status": "active", "result": ""},
                 {"ticker": "KXFED-26DEC-HOLD", "yes_sub_title": "Hold", "rules_primary": "Resolves Yes if no change.",
                  "close_time": "2026-12-16T19:00:00Z", "yes_bid_dollars": None, "yes_ask_dollars": None, "last_price_dollars": None, "status": "active", "result": ""}]},
    {"event_ticker": "KXNFL-1", "category": "Sports", "title": "Game", "markets": [{"ticker": "KXNFL-1-A", "close_time": "2026-12-01T00:00:00Z", "yes_bid_dollars": "0.5", "yes_ask_dollars": "0.52"}]}],
    "cursor": ""}
M_NOW = 1794300000.0      # 2026-11-10


def test_markets_refresh_search_and_skip_sports_and_unquoted(conn):
    calls = []
    assert fc.refresh_markets(conn, get=lambda u: calls.append(u) or EVENTS, now=M_NOW) == 2
    assert "with_nested_markets=true" in calls[0] and len(calls) == 1
    hits = fc.search_markets(conn, "Fed rate cut December", now=M_NOW)
    assert [h["ticker"] for h in hits] == ["KXFED-26DEC-CUT25"] and hits[0]["price"] == pytest.approx(0.62) and hits[0]["closes"] == "2026-12-16"
    assert fc.search_markets(conn, "NFL game", now=M_NOW) == [] and fc.search_markets(conn, "!!", now=M_NOW) == []
    assert fc.search_markets(conn, "Fed", now=M_NOW + 60 * DAY) == []       # closed markets are not offered
    assert fc.search_markets(conn, "Fed", now=M_NOW, max_days=7) == []      # ...nor ones that close beyond the window
    assert [h["ticker"] for h in fc.search_markets(conn, "Fed zebra", now=M_NOW)] == ["KXFED-26DEC-CUT25"]   # no all-word match: any-word fallback


def test_market_price_ignores_wide_or_one_sided_quotes():
    assert fc.market_price({"yes_bid": 0.6, "yes_ask": 0.64, "last": 0.2}) == pytest.approx(0.62)
    assert fc.market_price({"yes_bid": 0.1, "yes_ask": 0.9, "last": 0.4}) == 0.4            # too wide: fall back to last trade
    assert fc.market_price({"yes_bid": None, "yes_ask": 0.9, "last": None}) is None
    assert fc.market_price({"yes_bid": 0.0, "yes_ask": 1.0, "last": 1.0}) is None


def test_market_forecast_uses_the_market_price_as_baseline_and_resolves_from_the_exchange(conn):
    fc.refresh_markets(conn, get=lambda u: EVENTS, now=M_NOW)
    r = fc.add_market(conn, "KXFED-26DEC-CUT25", 0.8, "labour data", ["macro"], now=M_NOW)
    assert r["baseline"] == 0.62 and r["question"] == "Fed decision in December — Cut 25bps"
    with pytest.raises(ValueError, match="already an open forecast"): fc.add_market(conn, "KXFED-26DEC-CUT25", 0.7, now=M_NOW)
    with pytest.raises(ValueError, match="no usable quote"): fc.add_market(conn, "KXFED-26DEC-HOLD", 0.3, now=M_NOW)
    with pytest.raises(ValueError, match="find_markets"): fc.add_market(conn, "NOPE", 0.3, now=M_NOW)
    fc.refresh_markets(conn, get=lambda u: {"events": [], "cursor": ""}, now=M_NOW + DAY)
    assert conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1                  # a market with an open forecast is kept
    due = conn.execute("SELECT due_at FROM forecasts").fetchone()[0]
    result = {"market": {"result": ""}}
    assert fc.resolve(conn, FakeProvider(), get=lambda u: result, now=due + 60) == 0        # closed but not settled yet
    def down(u): raise OSError("net")
    assert fc.resolve(conn, FakeProvider(), get=down, now=due + 60) == 0
    seen = []
    assert fc.resolve(conn, FakeProvider(), get=lambda u: seen.append(u) or {"market": {"result": "yes"}}, now=due + 3600) == 1
    assert seen[0].endswith("/markets/KXFED-26DEC-CUT25") and conn.execute("SELECT outcome FROM forecasts").fetchone()[0] == 1


def test_scores_reward_beating_the_baseline_not_matching_it(conn):
    fc.ensure(conn)
    rows = [("price", 0.8, 0.5, 1, '["chokepoint"]'), ("price", 0.2, 0.5, 0, '["chokepoint"]'), ("market", 0.9, 0.6, 0, '[]'), ("market", 0.5, 0.5, 1, '[]')]
    for i, (kind, p, b, o, tags) in enumerate(rows):
        conn.execute("INSERT INTO forecasts(created_at, kind, ref, question, due_at, prob, baseline, tags, outcome) VALUES (1,?,?,?,2,?,?,?,?)", (kind, f"R{i}", "q", p, b, tags, o))
    conn.execute("INSERT INTO forecasts(created_at, kind, ref, question, due_at, prob, baseline) VALUES (1,'price','OPEN','q',9e9,0.5,0.5)"); conn.commit()
    by_kind = {r["group"]: r for r in fc.scores(conn, "kind")}
    assert by_kind["price"]["brier"] == pytest.approx(0.04) and by_kind["price"]["baseline_brier"] == pytest.approx(0.25) and by_kind["price"]["skill"] == pytest.approx(0.84)
    assert by_kind["market"]["skill"] < 0 and by_kind["price"]["hit"] == 1.0                # confidently wrong is punished
    assert {r["group"] for r in fc.scores(conn, "tag")} == {"chokepoint", "(untagged)"} and fc.scores(conn)[0]["n"] == 4
    text = fc.scores_text(conn)
    assert "all: n=4" in text and "1 still open" in text
    empty = __import__("thal.db", fromlist=["x"]).connect(":memory:")
    assert fc.scores_text(empty) == "no forecasts resolved yet (0 open)"
