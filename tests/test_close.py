import pytest
from thal import forecasts, ledger, memory
from thal.agent import Session
from thal.broker import PaperBroker
from thal.prices import FakeProvider
from thal.risk import Limits
from thal.thesis import BetOut, close_expired, close_position, place_bet
from thal.wiki import Wiki
from .conftest import DAY, T0, series

BARS = 60
NOW = T0 + BARS * DAY                 # every bar below is a complete session by now


@pytest.fixture
def world(conn, tmp_path):
    prov = FakeProvider({"SPY": series(100, 0.0, BARS), "XLE": series(50, 0.0, BARS), "UP": series(10, 0.01, BARS)})
    return conn, Wiki(tmp_path / "wiki"), prov, PaperBroker(conn, prov, Limits(kill_switch="/nonexistent/KILL"), starting_cash=100_000)


def now_mark(conn, bet_id, horizon="now"):
    r = conn.execute("SELECT ret FROM marks WHERE bet_id = ? AND horizon = ?", (bet_id, horizon)).fetchone()
    return None if r is None else r[0]


def test_a_closed_bet_is_scored_at_its_exit_with_no_look_ahead(world):
    conn, _, prov, _ = world
    b = ledger.add_bet(conn, symbol="UP", side="LONG", created_at=T0 - 3600, benchmark="XLE")     # enters at bar 0
    assert ledger.close_bet(conn, b, "invalidation   met", T0 + 9.5 * DAY) and not ledger.close_bet(conn, b, "again", NOW)
    ledger.mark_all(conn, prov, NOW)
    assert now_mark(conn, b) == pytest.approx(1.01 ** 10 - 1)              # exit at bar 10: the first session opening after the decision
    assert now_mark(conn, b, "4w") == pytest.approx(1.01 ** 20 - 1)        # what holding on would have done is still recorded
    row = conn.execute("SELECT closed_reason FROM bets").fetchone()
    assert row[0] == "invalidation met" and ledger.open_bets(conn) == []


def test_exit_waits_for_a_complete_session_and_a_bet_closed_before_entry_is_flat(world):
    conn, _, prov, _ = world
    b = ledger.add_bet(conn, symbol="UP", side="SHORT", created_at=T0 - 3600)
    ledger.close_bet(conn, b, "x", T0 + 9.5 * DAY)
    ledger.mark_all(conn, prov, T0 + 10 * DAY + 3600)                      # the exit session (bar 10) is still trading
    assert now_mark(conn, b) == pytest.approx(-(1.01 ** 9 - 1))            # provisional: marked to the last complete bar
    ledger.mark_all(conn, prov, NOW)
    assert now_mark(conn, b) == pytest.approx(-(1.01 ** 10 - 1))
    early = ledger.add_bet(conn, symbol="UP", side="LONG", created_at=T0 + 20.5 * DAY)
    ledger.close_bet(conn, early, "changed my mind the same afternoon", T0 + 20.6 * DAY)
    ledger.mark_all(conn, prov, NOW)
    assert now_mark(conn, early) == 0.0


def test_closing_a_paper_bet_sells_what_it_bought(world):
    conn, _, prov, broker = world
    t = T0 + 30.5 * DAY
    placed = place_bet(conn, prov, broker, BetOut(symbol="UP", side="LONG", thesis="t", invalidation="i", horizon_days=20, conviction=5), None, t)
    assert placed["order"] == "placed" and placed["qty"] > 0
    broker.settle(t + 2 * DAY)
    assert broker.portfolio(t + 2 * DAY).positions == {"UP": placed["qty"]}
    done = close_position(conn, broker, placed["bet"], "thesis broke", t + 5 * DAY)
    assert done["order"] == "placed" and close_position(conn, broker, placed["bet"], "again", t + 6 * DAY) is None
    broker.settle(NOW)
    assert broker.portfolio(NOW).positions == {}
    shadow = place_bet(conn, prov, broker, BetOut(symbol="UP", side="SHORT", thesis="t", invalidation="i", horizon_days=20, conviction=0), None, t)
    assert close_position(conn, broker, shadow["bet"], "done", t + DAY)["order"] == "none (nothing held)"


def test_bets_close_themselves_at_their_horizon_but_the_old_bots_are_left_alone(world):
    conn, _, prov, broker = world
    a = ledger.add_bet(conn, symbol="UP", side="LONG", created_at=T0, horizon_days=28)
    ledger.add_bet(conn, symbol="UP", side="LONG", created_at=T0, horizon_days=90)
    ledger.add_bet(conn, symbol="UP", side="LONG", created_at=T0, horizon_days=5, source="v1", kind="alert")
    assert close_expired(conn, broker, T0 + 27 * DAY) == 0
    assert close_expired(conn, broker, T0 + 28 * DAY) == 1 and close_expired(conn, broker, T0 + 29 * DAY) == 0
    r = conn.execute("SELECT closed_at, closed_reason FROM bets WHERE id = ?", (a,)).fetchone()
    assert r[0] == T0 + 28 * DAY and r[1] == "horizon reached"
    assert conn.execute("SELECT COUNT(*) FROM bets WHERE closed_at IS NULL").fetchone()[0] == 2


def test_analyst_can_read_and_close_its_bets_and_sees_them_on_the_dashboard(world):
    conn, wiki, prov, broker = world
    fuse = memory.open_fuse(conn, "Diesel premium deflates", "thesis", ["crack below 80"], now=T0)
    s0 = Session(conn, wiki, prov, broker, T0 + 30.5 * DAY)
    assert "recorded as bet #1" in s0.propose_bet(symbol="UP", side="LONG", thesis="Cracks fall.", invalidation="Crack back above $100.",
                                                 horizon_days=28, conviction=0, benchmark="XLE", fuse_id=fuse)
    old = ledger.add_bet(conn, symbol="UP", side="LONG", source="v1", kind="alert", created_at=T0)
    ledger.mark_all(conn, prov, T0 + 40 * DAY)
    s = Session(conn, wiki, prov, broker, T0 + 40.5 * DAY)
    dash = memory.dashboard(conn, wiki, T0 + 40.5 * DAY)
    assert "## Open bets (1)" in dash and "- #1 LONG UP vs XLE [day 10 of 28, +" in dash and "fuse #1] wrong if: Crack back above $100." in dash
    assert "1 open bets]" in dash                                           # the fuse line shows how much already rides on it
    text = s.read_bet(1)
    assert "Bet #1 [open] LONG UP vs XLE" in text and "Thesis: Cracks fall." in text and "fuse #1" in text and "no bet #9" == s.read_bet(9)
    assert "give the specific reason" in s.close_bet(1, "  ") and f"no open bet #{old}" == s.close_bet(old, "not mine")
    assert s.close_bet(1, "Crack rebounded to $104 after a refinery strike.").startswith("closed bet #1 (LONG UP)")
    assert "already closed" in s.close_bet(1, "again") and s.counts["bets_closed"] == 1
    assert "[closed" in s.read_bet(1) and "Crack rebounded" in s.read_bet(1)
    assert "## Open bets (0)" in memory.dashboard(conn, wiki, T0 + 41 * DAY)
    assert s.call("close_bet", {"bet_id": 1, "reason": "x"})[1] is False and {"close_bet", "read_bet"} <= {t["name"] for t in __import__("thal.agent", fromlist=["TOOLS"]).TOOLS}


def test_forecasts_record_the_fuse_they_come_from(world):
    conn, wiki, prov, broker = world
    fuse = memory.open_fuse(conn, "t", "thesis", ["w"], now=T0)
    s = Session(conn, wiki, prov, broker, NOW)
    s.forecast_price("UP", "above", 20, 5, 0.6, "r", fuse_id=fuse)
    s.forecast_price("UP", "above", 21, 5, 0.6, "r", fuse_id=999)          # a fuse that does not exist is dropped, not trusted
    assert [r[0] for r in conn.execute("SELECT fuse_id FROM forecasts ORDER BY id")] == [fuse, None]


def test_dashboard_caps_the_open_bet_list(world, monkeypatch):
    conn, wiki, _, _ = world
    monkeypatch.setattr(memory, "MAX_BETS_SHOWN", 3)
    for i in range(5):
        ledger.add_bet(conn, symbol=f"S{i}", side="LONG", created_at=T0 + i, horizon_days=28, invalidation="x")
    dash = memory.dashboard(conn, wiki, T0 + DAY)
    assert "## Open bets (5)" in dash and "... and 2 newer ones; use read_bet" in dash and "#4 LONG S3" not in dash
