import pytest
from thal import ledger
from thal.prices import FakeProvider, complete, entry_index, price_on_or_before
from .conftest import DAY, T0, series

NOW = T0 + 200 * DAY


def marks(conn, bet_id):
    return {r["horizon"]: r for r in conn.execute("SELECT * FROM marks WHERE bet_id = ?", (bet_id,))}


def test_entry_is_first_session_after_the_idea_never_the_same_one():
    bars = series(100, 0.01, 10)
    assert entry_index(bars, T0 - 1) == 0            # issued before the open -> that session's close
    assert entry_index(bars, T0) == 1                # issued at the open -> next session
    assert entry_index(bars, T0 + 3600) == 1         # issued mid-session -> next session, no look-ahead
    assert entry_index(bars, bars[-1][0]) is None    # nothing after the last bar yet


def test_incomplete_session_is_not_used():
    bars = series(100, 0.0, 3)
    assert len(complete(bars, bars[-1][0] + 3600)) == 2      # last session still trading
    assert len(complete(bars, bars[-1][0] + 21 * 3600)) == 3


def test_long_marks_at_each_horizon(conn):
    p = FakeProvider({"AAA": series(100, 0.01, 100), "SPY": series(100, 0.0, 100)})
    b = ledger.add_bet(conn, symbol="AAA", side="long", created_at=T0 - 1)
    assert ledger.mark_all(conn, p, NOW) == 4
    m = marks(conn, b)
    assert m["1w"]["ret"] == pytest.approx(1.01 ** 5 - 1)
    assert m["4w"]["ret"] == pytest.approx(1.01 ** 20 - 1)
    assert m["12w"]["ret"] == pytest.approx(1.01 ** 60 - 1)
    assert m["now"]["ret"] == pytest.approx(1.01 ** 99 - 1)
    assert m["1w"]["vs_spy"] == pytest.approx(m["1w"]["ret"])     # SPY flat
    row = conn.execute("SELECT entry_px, entry_at FROM bets WHERE id = ?", (b,)).fetchone()
    assert row["entry_px"] == pytest.approx(100) and row["entry_at"] == T0


def test_short_is_signed_and_benchmark_relative(conn):
    p = FakeProvider({"AAA": series(100, 0.01, 100), "SPY": series(100, 0.01, 100), "XLE": series(10, 0.02, 100)})
    b = ledger.add_bet(conn, symbol="AAA", side="SHORT", created_at=T0 - 1, benchmark="XLE")
    ledger.mark_all(conn, p, NOW)
    m = marks(conn, b)["4w"]
    assert m["ret"] == pytest.approx(-(1.01 ** 20 - 1))           # short a rising stock loses
    assert m["vs_spy"] == pytest.approx(0.0)                      # ...but matched a short of the market
    assert m["vs_bench"] == pytest.approx(-(1.01 ** 20 - 1) + (1.02 ** 20 - 1))


def test_horizon_not_reached_is_left_unmarked(conn):
    p = FakeProvider({"AAA": series(100, 0.01, 12), "SPY": series(100, 0.0, 12)})
    b = ledger.add_bet(conn, symbol="AAA", side="LONG", created_at=T0 - 1)
    ledger.mark_all(conn, p, NOW)
    assert set(marks(conn, b)) == {"1w", "now"}


def test_bet_with_no_bar_after_it_gets_no_marks_and_no_entry(conn):
    p = FakeProvider({"AAA": series(100, 0.01, 5), "SPY": series(100, 0.0, 5)})
    b = ledger.add_bet(conn, symbol="AAA", side="LONG", created_at=T0 + 10 * DAY)
    assert ledger.mark_all(conn, p, NOW) == 0
    assert conn.execute("SELECT entry_px FROM bets WHERE id = ?", (b,)).fetchone()["entry_px"] is None


def test_unknown_symbol_is_skipped_not_fatal(conn, flat_market):
    ledger.add_bet(conn, symbol="NOPE", side="LONG", created_at=T0)
    assert ledger.mark_all(conn, flat_market, NOW) == 0


def test_spread_is_long_leg_minus_hedge(conn):
    p = FakeProvider({"ZS=F": series(100, 0.02, 40), "ZC=F": series(100, 0.01, 40), "SPY": series(1, 0, 40)})
    b = ledger.add_bet(conn, symbol="ZS=F", hedge_symbol="ZC=F", side="LONG", created_at=T0 - 1)
    ledger.mark_all(conn, p, NOW)
    assert marks(conn, b)["4w"]["ret"] == pytest.approx((1.02 ** 20) - (1.01 ** 20))
    assert conn.execute("SELECT asset_class FROM bets").fetchone()[0] == "future"


def test_remarking_is_idempotent(conn):
    p = FakeProvider({"AAA": series(100, 0.01, 30), "SPY": series(100, 0.0, 30)})
    ledger.add_bet(conn, symbol="AAA", side="LONG", created_at=T0 - 1)
    ledger.mark_all(conn, p, NOW); ledger.mark_all(conn, p, NOW)
    assert conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 3


def test_bad_bets_are_rejected(conn):
    with pytest.raises(ValueError): ledger.add_bet(conn, symbol="AAA", side="HOLD")
    with pytest.raises(ValueError): ledger.add_bet(conn, symbol="  ", side="LONG")


def test_benchmark_uses_calendar_day_not_exact_timestamp():
    spy = [(T0 - 9 * 3600, 100.0), (T0 - 9 * 3600 + DAY, 110.0)]      # a market that opens earlier in the day
    assert price_on_or_before(spy, T0) == 100.0
    assert price_on_or_before(spy, T0 + DAY) == 110.0
    assert price_on_or_before(spy, T0 - 2 * DAY) is None


def test_scoreboard_counts_repeats_once_and_groups(conn):
    p = FakeProvider({"UP": series(100, 0.01, 40), "DN": series(100, -0.01, 40), "SPY": series(100, 0.0, 40)})
    for i in range(5):                                           # the same idea issued five times
        ledger.add_bet(conn, symbol="UP", side="LONG", source="v1", kind="alert", created_at=T0 - 1 + i * DAY)
    ledger.add_bet(conn, symbol="DN", side="LONG", source="v1", kind="kill", created_at=T0 - 1, tags=["sugar"])
    ledger.mark_all(conn, p, NOW)
    uniq = {(r["group"], r["horizon"]): r for r in ledger.scoreboard(conn, by="kind")}
    assert uniq[("alert", "4w")]["n"] == 1 and uniq[("kill", "4w")]["n"] == 1
    assert uniq[("alert", "4w")]["win"] == 1.0 and uniq[("kill", "4w")]["win"] == 0.0
    every = {(r["group"], r["horizon"]): r for r in ledger.scoreboard(conn, by="kind", unique=False)}
    assert every[("alert", "1w")]["n"] == 5
    tagged = ledger.scoreboard(conn, by="tag")
    assert {r["group"] for r in tagged} == {"sugar", "(untagged)"}
    assert "vs SPY" in ledger.format_scoreboard(tagged)
    with pytest.raises(ValueError): ledger.scoreboard(conn, by="thesis; DROP TABLE bets")
