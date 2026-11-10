import math
import pytest
from thal import risk
from thal.broker import PaperBroker, get_broker
from thal.prices import FakeProvider, YahooProvider
from thal.risk import Limits, OrderRequest, Portfolio, check
from .conftest import DAY, T0, series

L = Limits(kill_switch="/nonexistent/KILL")
PF = Portfolio(cash=100_000)


def reasons(order, pf=PF, limits=L, n=0, env=None):
    return " | ".join(check(order, pf, limits, n, env or {}).reasons)


def test_small_buy_passes():
    assert check(OrderRequest("AAA", "BUY", 10, 100), PF, L, 0, {})


def test_position_limit_counts_what_is_already_held():
    pf = Portfolio(cash=100_000, positions={"AAA": 15}, prices={"AAA": 100})
    assert check(OrderRequest("AAA", "BUY", 5, 100), pf, L, 0, {})                    # 2,000 exactly
    assert "per-position limit" in reasons(OrderRequest("AAA", "BUY", 6, 100), pf)


def test_gross_limit():
    pf = Portfolio(cash=100_000, positions={f"S{i}": 19.5 for i in range(10)}, prices={f"S{i}": 100 for i in range(10)})
    assert "gross exposure" in reasons(OrderRequest("NEW", "BUY", 10, 100), pf)
    assert check(OrderRequest("NEW", "BUY", 5, 100), pf, L, 0, {})


def test_position_count_limit():
    pf = Portfolio(cash=100_000, positions={f"S{i}": 1 for i in range(15)}, prices={f"S{i}": 10 for i in range(15)})
    assert "over the limit of 15" in reasons(OrderRequest("NEW", "BUY", 1, 10), pf)
    assert check(OrderRequest("S3", "BUY", 1, 10), pf, L, 0, {})                      # adding to an existing one is fine


def test_daily_order_limit():
    assert "daily order limit" in reasons(OrderRequest("AAA", "BUY", 1, 100), n=10)
    assert check(OrderRequest("AAA", "BUY", 1, 100), PF, L, 9, {})


def test_shorts_blocked_unless_enabled_including_flipping_through_zero():
    assert "short positions" in reasons(OrderRequest("AAA", "SELL", 5, 100))
    pf = Portfolio(cash=100_000, positions={"AAA": 5}, prices={"AAA": 100})
    assert "short positions" in reasons(OrderRequest("AAA", "SELL", 8, 100), pf)       # 5 long -> 3 short
    allow = Limits(allow_short=True, kill_switch="/nonexistent/KILL")
    assert check(OrderRequest("AAA", "SELL", 5, 100), PF, allow, 0, {})


def test_reducing_is_allowed_even_when_over_every_limit():
    pf = Portfolio(cash=0, positions={"AAA": 1000}, prices={"AAA": 100})             # way over the limits
    assert check(OrderRequest("AAA", "SELL", 400, 100), pf, L, 99, {})
    assert check(OrderRequest("AAA", "SELL", 1000, 100), pf, L, 99, {})
    assert not check(OrderRequest("AAA", "BUY", 1, 100), pf, L, 0, {})


def test_asset_class_blocked_symbols_and_cash():
    assert "asset class" in reasons(OrderRequest("ZC=F", "BUY", 1, 100, "future"))
    assert "blocked" in reasons(OrderRequest("BAD", "BUY", 1, 100), limits=Limits(blocked_symbols=("BAD",), kill_switch="/x"))
    assert "not enough cash" in reasons(OrderRequest("AAA", "BUY", 10, 100), Portfolio(cash=500))


@pytest.mark.parametrize("qty,price", [(0, 100), (-5, 100), (math.nan, 100), (math.inf, 100), (5, 0), (5, math.nan), (True, 100), ("5", 100)])
def test_garbage_numbers_are_rejected(qty, price):
    assert not check(OrderRequest("AAA", "BUY", qty, price), PF, L, 0, {})


def test_unknown_action_rejected():
    assert "unknown action" in reasons(OrderRequest("AAA", "HOLD", 1, 100))


def test_kill_switch_stops_everything_even_reductions(tmp_path):
    k = tmp_path / "KILL"; k.write_text("stop")
    lim = Limits(kill_switch=str(k))
    pf = Portfolio(cash=0, positions={"AAA": 10}, prices={"AAA": 100})
    assert reasons(OrderRequest("AAA", "SELL", 5, 100), pf, lim) == "kill switch is on"


def test_live_needs_both_switches():
    live = Limits(mode="live", kill_switch="/nonexistent/KILL")
    o = OrderRequest("AAA", "BUY", 1, 100)
    assert "THAL_LIVE" in reasons(o, limits=live, env={})
    assert "THAL_LIVE" in reasons(o, limits=live, env={"THAL_LIVE": "1"})
    assert check(o, PF, live, 0, {"THAL_LIVE": "yes"})


def test_limits_validation_and_loading(tmp_path):
    with pytest.raises(ValueError): Limits(mode="yolo")
    with pytest.raises(ValueError): Limits(max_position_usd=-1)
    with pytest.raises(ValueError): Limits(max_position_usd=math.inf)
    with pytest.raises(ValueError): Limits(max_position_usd=50_000, max_gross_usd=20_000)
    assert risk.load(tmp_path / "missing.yaml") == Limits()
    p = tmp_path / "r.yaml"
    p.write_text("mode: paper\nmax_positon_usd: 999999\n")                         # typo
    with pytest.raises(ValueError, match="unknown risk settings"): risk.load(p)
    p.write_text("max_position_usd: 500\nallowed_asset_classes: [equity, future]\n")
    lim = risk.load(p)
    assert lim.max_position_usd == 500 and lim.allowed_asset_classes == ("equity", "future")
    assert risk.load().mode == "paper"                                             # the shipped config is paper


# ---- paper broker ----

def broker(conn, n=30, **px):
    data = {"SPY": series(100, 0.0, n)}
    data.update({k: series(v, 0.01, n) for k, v in px.items()})
    return PaperBroker(conn, FakeProvider(data), L, starting_cash=10_000)


def test_order_fills_at_next_complete_close_not_the_current_one(conn):
    b = broker(conn, AAA=100)
    t = T0 + 5 * DAY + 3600                                   # during session 5
    oid, d = b.submit("AAA", "BUY", 10, now=t)
    assert d and b.settle(now=t) == 0                         # nothing to fill against yet
    assert b.settle(now=T0 + 6 * DAY + 3600) == 0             # session 6 is still trading
    assert b.settle(now=T0 + 7 * DAY) == 1
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    assert o["status"] == "filled" and o["fill_px"] == pytest.approx(100 * 1.01 ** 6) and o["fill_at"] == T0 + 6 * DAY


def test_cash_positions_and_equity_reconcile(conn):
    b = broker(conn, AAA=100)
    b.submit("AAA", "BUY", 10, now=T0 + DAY + 3600); b.settle(now=T0 + 4 * DAY)
    pf = b.portfolio(now=T0 + 10.9 * DAY)
    fill = 100 * 1.01 ** 2
    assert pf.positions == {"AAA": 10} and pf.cash == pytest.approx(10_000 - 10 * fill)
    assert pf.equity == pytest.approx(10_000 + 10 * (100 * 1.01 ** 10 - fill))
    b.submit("AAA", "SELL", 10, now=T0 + 10.9 * DAY); b.settle(now=T0 + 13 * DAY)
    pf = b.portfolio(now=T0 + 13 * DAY)
    assert pf.positions == {} and pf.cash == pytest.approx(10_000 + 10 * (100 * 1.01 ** 11 - fill))


def test_pending_orders_count_toward_limits(conn):
    b = broker(conn, AAA=100)
    t = T0 + 3 * DAY
    assert b.submit("AAA", "BUY", 15, now=t)[1]
    oid, d = b.submit("AAA", "BUY", 15, now=t)                # second burst order before the first fills
    assert not d and "per-position limit" in d.reasons[0]
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0] == "rejected"


def test_rejections_are_recorded_and_never_fill(conn):
    b = broker(conn, AAA=100)
    oid, d = b.submit("NOPRICE", "BUY", 1, now=T0 + 3 * DAY)
    assert not d and "reference price" in d.reasons[0]
    b.submit("AAA", "BUY", float("nan"), now=T0 + 3 * DAY)
    assert b.settle(now=T0 + 20 * DAY) == 0
    assert b.portfolio(now=T0 + 20 * DAY).positions == {}


def test_daily_order_cap_in_broker(conn):
    lim = Limits(max_orders_per_day=2, kill_switch="/nonexistent/KILL")
    b = PaperBroker(conn, FakeProvider({"AAA": series(10, 0, 30), "BBB": series(10, 0, 30), "CCC": series(10, 0, 30)}), lim)
    t = T0 + 3 * DAY
    assert b.submit("AAA", "BUY", 1, now=t)[1] and b.submit("BBB", "BUY", 1, now=t)[1]
    assert not b.submit("CCC", "BUY", 1, now=t)[1]
    assert b.submit("CCC", "BUY", 1, now=t + DAY)[1]          # a new day


def test_starting_cash_persists_and_sizing(conn):
    b = broker(conn, AAA=100)
    again = PaperBroker(conn, b.provider, L, starting_cash=999)
    assert again.starting_cash == 10_000
    assert b.shares_for("AAA", 1000, now=T0 + 1.9 * DAY) == 9          # 1000 // 101
    assert b.shares_for("NOPRICE", 1000, now=T0 + 2 * DAY) == 0


def test_live_mode_has_no_broker_yet(conn):
    with pytest.raises(ValueError): PaperBroker(conn, FakeProvider(), Limits(mode="live"))
    with pytest.raises(NotImplementedError): get_broker(conn, FakeProvider(), Limits(mode="live"))


# ---- yahoo cache ----

def payload(n, px=10.0):
    return {"chart": {"result": [{"timestamp": [T0 + i * DAY for i in range(n)],
                                  "indicators": {"adjclose": [{"adjclose": [px + i for i in range(n - 1)] + [None]}]}}]}}


def test_yahoo_caches_skips_nulls_and_survives_outages(conn):
    calls = []
    def fetch(url): calls.append(url); return payload(5)
    y = YahooProvider(conn, fetch=fetch)
    assert len(y.bars("ZC=F", now=T0 + 10 * DAY)) == 4                 # the null close is dropped
    assert "ZC%3DF" in calls[0]
    y.bars("ZC=F", now=T0 + 10 * DAY + 60)
    assert len(calls) == 1                                            # served from cache
    def down(url): raise OSError("offline")
    y2 = YahooProvider(conn, fetch=down)
    assert len(y2.bars("ZC=F", now=T0 + 20 * DAY)) == 4                # stale but kept
    assert y2.bars("NEW", now=T0 + 20 * DAY) == []
