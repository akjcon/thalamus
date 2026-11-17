"""The strategy lab: rules are data, code tests them, and the gates are enforced in code: a development test against
placebo versions of the rule that tightens with every rule tried, one run on a holdout the proposer never sees, then
a live shadow record."""
import calendar
import json
from datetime import date, timedelta
import pytest
from thal import lab, ledger, oversee, serve, ui
from thal.lab import Prices, Proposed, Rule
from thal.llm import LLM
from thal.prices import FakeProvider
from .fakes import FakeClient

DAY = 86400.0
START, END = date(2015, 1, 1), date(2026, 11, 10)
NOW = float(calendar.timegm((2026, 11, 12, 7, 0, 0)))
ts = lambda d: float(calendar.timegm(d.timetuple())) + 14.5 * 3600          # that day's session open


@pytest.fixture(autouse=True)
def fewer_placebos(monkeypatch):
    monkeypatch.setattr(lab, "PLACEBOS", 300)


def world(conn):
    """A gauge that sits at 100 and drops to 40 for five days at irregular intervals (31 to 53 days apart), and a
    stock that rises 1% a day for the ten days starting three days after each drop. Otherwise it wobbles around a
    flat line, and so does the market."""
    n = (END - START).days + 1
    days = [START + timedelta(days=i) for i in range(n)]
    drops, i, k = [], 40, 0
    while i < n - 20:
        drops.append(i); k += 1; i += 31 + (k * 17) % 23
    low = {j for d in drops for j in range(d, d + 5)}
    rise = {j for d in drops for j in range(d + 3, d + 13)}
    conn.executemany("INSERT INTO readings VALUES ('Gauge', ?, ?, ?, 'cm')", [(d.isoformat(), ts(d), 40.0 if i in low else 100.0) for i, d in enumerate(days)])
    conn.execute("INSERT INTO sources(name, url, added_at, kind, why) VALUES ('Gauge', 'https://g.example', 1, 'series', 'low again in October 2026')")
    conn.commit()
    px, bars = 100.0, []
    for i, d in enumerate(days):
        px *= 1.01 if i in rise else (1.002 if i % 2 else 1 / 1.002)
        bars.append((ts(d), px))
    data = {"BARGE": bars, "SPY": [(ts(d), 50.0) for d in days], "FLAT": [(ts(d), 20.0 + (i % 3) * 0.01) for i, d in enumerate(days)]}
    return (lambda sym: data.get(sym, [])), [days[d] for d in drops]


def rule(**kw):
    return Rule(**{**dict(name="Low water lifts barge rates", hypothesis="Below 50 cm barges load light, so rates rise for weeks.", series="Gauge",
                          stat="level", op="below", value=50, symbol="barge", side="LONG", hold_days=10), **kw})


def test_a_rule_is_tested_only_on_what_was_known_and_entered_after_a_lag(conn):
    prices, drops = world(conn)
    dev, held, _ = lab.evaluate(conn, rule(), prices)
    first = dev[0]
    assert first["signal"] == drops[0].isoformat() == "2015-02-10" and first["exit"] == "2015-02-22"     # in at the close two days later, out ten days on
    assert first["excess"] == pytest.approx(1.01 ** 10 - 1 - lab.COST, abs=0.005)      # in before the rise starts, so all ten days of it are caught
    assert all(t["exit"] < lab.HOLDOUT_START for t in dev) and all(t["signal"] >= lab.HOLDOUT_START for t in held)
    assert len(dev) + len(held) in (len(drops), len(drops) - 1)                       # a trade across 1 July counts for neither
    assert len({t["signal"][:4] for t in dev}) == 12 and lab.summary(dev)["hit"] == 1.0
    assert lab.summary([]) == {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0}


def test_statistics_use_only_the_past(conn):
    world(conn)
    rows = lab.readings(conn, "Gauge")
    change = dict(lab.stat_series(rule(stat="change", window=1), rows))
    assert change[date(2015, 2, 10)] == -60 and change[date(2015, 2, 15)] == 60 and date(2015, 1, 1) not in change
    total = dict(lab.stat_series(rule(stat="sum", window=10), rows))
    assert total[date(2015, 2, 14)] == 5 * 40 + 5 * 100 and date(2015, 1, 5) not in total      # too few days for a 10-day total
    gap = [r for r in rows if r[0] != date(2015, 2, 12)]
    assert dict(lab.stat_series(rule(stat="sum", window=10), gap))[date(2015, 2, 14)] == pytest.approx((4 * 40 + 5 * 100) * 10 / 9)   # a missing day is not a zero
    normal = lab.stat_series(rule(versus="normal"), rows)
    assert normal[0][0].year == 2020 and all(abs(x) <= 60 for _, x in normal)                  # needs five earlier years of the same date
    assert lab.crossings(rule(), [(date(2020, 1, 1), 60), (date(2020, 1, 2), 40), (date(2020, 1, 3), 30), (date(2020, 1, 4), 70), (date(2020, 1, 5), 45)]) \
        == [date(2020, 1, 2), date(2020, 1, 5)]                                                # the day it becomes true, not every day it stays true


def test_a_rule_must_beat_its_own_placebos_and_gets_no_credit_for_a_rising_price(conn):
    prices, drops = world(conn)
    dev, _, share = lab.evaluate(conn, rule(), prices, tried=1)
    assert share == pytest.approx(1 / 301)                                             # no slid version of it does as well
    # the same signals on something that simply goes up 0.1% a day: a fine return over a flat market, and no timing at all
    up = [(t, 10 * 1.001 ** i) for i, (t, _) in enumerate(prices("SPY"))]
    rising = lambda sym: up if sym == "UP" else prices(sym)
    trend, _, luck = lab.evaluate(conn, rule(symbol="UP"), rising, tried=1)
    assert lab.summary(trend)["mean"] > 0.005 and lab.summary(trend)["t"] > 50 and luck > 0.3     # a t-test would have passed it
    assert lab.bar(1) == 0.05 and lab.bar(10) == 0.005 and lab.bar(0) == 0.05


def test_each_rule_is_tried_once_and_the_test_tightens_with_every_rule(conn):
    prices, _ = world(conn)
    good = lab.record(conn, rule(), prices, NOW)
    assert good["status"] == "passed_dev" and good["years"] == 12 and good["p"] <= good["bar"] == 0.05
    assert lab.record(conn, rule(name="Same rule, new name", hypothesis="x"), prices, NOW) == {"name": "Same rule, new name", "status": "duplicate"}
    for same in (rule(symbol="BARGE"), rule(benchmark="spy"), rule(window=7)):         # one rule, however it is spelled
        assert lab.record(conn, same, prices, NOW)["status"] == "duplicate"
    never = lab.record(conn, rule(name="Never fires", op="above", value=500), prices, NOW)
    wrong = lab.record(conn, rule(name="Wrong way", side="SHORT"), prices, NOW)
    assert (never["status"], never["n"], wrong["status"]) == ("failed_dev", 0, "failed_dev") and wrong["mean"] < 0
    bad = {"No series": rule(series="Volga"), "No prices": rule(symbol="NOPE"), "Too long": rule(hold_days=400), "Bad benchmark": rule(benchmark="NOPE"),
           "Against itself": rule(benchmark="Barge"), "A future": rule(symbol="ZC=F"), "Future as benchmark": rule(benchmark="CL=F")}
    for name, r in bad.items():
        assert lab.record(conn, r.model_copy(update={"name": name}), prices, NOW)["status"] == "invalid", name
    notes = {r["name"]: r["note"] for r in conn.execute("SELECT name, note FROM strategies")}
    assert notes["Never fires"] == "only 0 trades" and notes["Wrong way"] == "lost to its benchmark" and "at least two years" in notes["No series"]
    assert "use the commodity's fund" in notes["A future"]
    def down(sym): raise lab.Unavailable("prices down")
    before = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert lab.record(conn, rule(name="Unlucky", hold_days=11), down, NOW)["status"] == "postponed"     # an outage settles nothing
    assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == before and lab.holdout(conn, down, NOW) == []
    conn.executemany("INSERT INTO strategies(created_at, key, name, hypothesis, spec, status) VALUES (1, ?, 'x', 'h', '{}', 'failed_dev')", [(f"k{i}",) for i in range(200)])
    late = lab.record(conn, rule(name="Late, with a short hold", hold_days=9), prices, NOW)
    assert late["bar"] == round(0.05 / 204, 5) and late["status"] == "failed_dev"       # 3 tried + 200 + this one; invalid ones do not count
    assert "of placebo versions did as well" in conn.execute("SELECT note FROM strategies WHERE name LIKE 'Late%'").fetchone()[0]


def test_a_series_must_be_daily_and_long_enough_for_what_the_rule_asks(conn):
    prices, _ = world(conn)
    conn.executemany("INSERT INTO readings VALUES ('Monthly', ?, 1, 5, 'kt')", [(f"{y}-{m:02d}-01",) for y in range(2015, 2026) for m in range(1, 13)])
    conn.executemany("INSERT INTO readings VALUES ('Young', ?, 1, 5, 'm')", [((date(2023, 1, 1) + timedelta(days=i)).isoformat(),) for i in range(1200)])
    conn.commit()
    assert "not a daily series" in lab._problem(conn, rule(series="Monthly"), prices)
    assert lab._problem(conn, rule(series="Young"), prices) == "" and "seasonal normal" in lab._problem(conn, rule(series="Young", versus="normal"), prices)
    assert "Monthly" in lab.catalog(conn)      # listed; the rule check is what refuses it


def test_holdout_is_run_once_and_then_the_rule_trades_live_as_shadow_bets(conn, monkeypatch):
    prices, drops = world(conn)
    monkeypatch.setattr(lab, "MIN_HOLDOUT_TRADES", 3)                                   # the made-up record has three trades after 1 July
    lab.record(conn, rule(), prices, NOW)
    lab.record(conn, rule(name="Loses later", symbol="FADE"), lambda s: prices("BARGE" if s == "FADE" else s), NOW)
    falls = lambda s: [(t, 100.0 - i * 0.001) for i, (t, _) in enumerate(prices("SPY"))] if s == "FADE" else prices(s)
    monkeypatch.setattr(lab, "MIN_HOLDOUT_TRADES", 8)
    assert lab.holdout(conn, falls, NOW) == []                                          # too few trades there yet: it waits, and is not judged on three
    monkeypatch.setattr(lab, "MIN_HOLDOUT_TRADES", 3)
    done = {d["name"]: d for d in lab.holdout(conn, falls, NOW)}
    assert done["Low water lifts barge rates"]["status"] == "live" and done["Low water lifts barge rates"]["n"] >= 3
    assert done["Loses later"]["status"] == "failed_holdout"
    assert lab.holdout(conn, falls, NOW) == []                                          # there is no second run
    assert conn.execute("SELECT holdout_at FROM strategies WHERE name = 'Loses later'").fetchone()[0] == NOW

    # nothing about the holdout reaches a model: not the proposer's lists, not the job's summary, not the overseer's report
    seen = lab.registry(conn) + lab.catalog(conn)
    assert seen.count("passed development") == 2 and "holdout" not in seen and "live" not in seen and "October 2026" not in seen
    said = lab.run(conn, None, FakeProvider({}), NOW, prices=falls)
    assert said == "Lab: 2 have passed development, 2 tried in all." and "- rules tried 2, passed development 2, could not be tested 0" in oversee.health(conn, NOW)

    provider = FakeProvider({"BARGE": prices("BARGE"), "SPY": prices("SPY")})
    assert lab.run_live(conn, provider, NOW) == []                                      # the last drop is too old to act on
    today = date(2026, 11, 12)
    conn.execute("UPDATE readings SET value = 40 WHERE source = 'Gauge' AND day = ?", ((today - timedelta(days=2)).isoformat(),)); conn.commit()
    conn.execute("INSERT INTO readings VALUES ('Gauge', ?, 1, 100, 'cm'), ('Gauge', ?, 1, 40, 'cm')", ((today - timedelta(days=1)).isoformat(), today.isoformat())); conn.commit()
    assert lab.run_live(conn, FakeProvider({}), NOW) == ["Low water lifts barge rates: LONG barge (not recorded: no recent price; not a tradeable instrument)"]
    assert lab.run_live(conn, provider, NOW) == ["Low water lifts barge rates: LONG barge"]         # the signal was not used up by the failure
    assert conn.execute("SELECT last_signal FROM strategies WHERE id = 1").fetchone()[0] == "2026-11-10"   # two days old; today's part-day reading is not acted on
    bet = conn.execute("SELECT * FROM bets").fetchone()
    assert (bet["source"], bet["kind"], bet["symbol"], bet["horizon_days"], bet["benchmark"]) == ("lab", "shadow", "BARGE", 10, "SPY")
    assert json.loads(bet["tags"]) == ["lab", "lab-1"] and "Lab rule #1" in bet["thesis"] and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert lab.run_live(conn, provider, NOW + 3600) == []                               # the same signal is acted on once
    assert ledger.open_bets(conn) == []                                                 # not among the analyst's bets to review

    # twenty closed live bets that made 0.1% before costs: a loss after them, and the rule is retired
    for i in range(20):
        conn.execute("INSERT INTO bets(created_at, source, kind, symbol, side, tags, closed_at) VALUES (?, 'lab', 'shadow', 'BARGE', 'LONG', '[\"lab\", \"lab-1\"]', ?)", (NOW, NOW))
        conn.execute("INSERT INTO marks(bet_id, horizon, ret, vs_bench, marked_at) VALUES (?, 'now', 0.001, 0.001, ?)", (conn.execute("SELECT MAX(id) FROM bets").fetchone()[0], NOW))
    conn.commit()
    assert lab.live_record(conn, 1) == {"n": 20, "mean": -0.001}
    conn.execute("INSERT INTO strategies(created_at, key, name, hypothesis, spec, status) VALUES (1, 'broken', 'x', 'h', '{}', 'live')"); conn.commit()
    lab.run_live(conn, provider, NOW + DAY)                                             # the unreadable rule does not stop the others
    assert [tuple(r) for r in conn.execute("SELECT status, note FROM strategies WHERE id = 1")] == [("retired", "retired: -0.1% against its benchmark, after costs, over 20 live bets")]
    assert "could not be shown" in ui.page(conn, NOW)


def test_the_proposer_sees_development_figures_only_and_the_job_reports(conn):
    prices, _ = world(conn)
    conn.execute("INSERT INTO readings VALUES ('kalshi:KXRAIN-4', '2020-01-01', 1, 0.3, 'prob')")
    conn.execute("INSERT INTO readings VALUES ('New gauge', '2026-01-01', 1, 5, 'm')"); conn.commit()
    listed = lab.catalog(conn)
    assert listed.startswith("- Gauge (cm; a level): daily since 2015-01-01") and "October 2026" not in listed   # not the source's own description
    assert "kalshi" not in listed and "New gauge" not in listed                          # too short a record to test anything on
    client = FakeClient(Proposed(rules=[rule(), rule(name="Never fires", op="above", value=500)]))
    out = lab.run(conn, LLM(conn, client), FakeProvider({}), NOW, proposing=True, prices=prices)
    assert out == "Lab: 2 rules proposed, 1 passed development (Low water lifts barge rates). 1 have passed development, 2 tried in all."
    sent = client.calls[0]
    assert sent["model"] == "claude-fable-5-2" and "at most 5.00% of its placebo versions may do as well" in sent["messages"][0]["content"]
    assert "(none yet)" in sent["messages"][0]["content"] and "2026-07-01" in sent["system"][0]["text"]
    assert lab.run(conn, LLM(conn, FakeClient()), FakeProvider({}), NOW + DAY, prices=prices) == "Lab: 1 have passed development, 2 tried in all."   # no model call on other days

    page = ui.page(conn, NOW)
    assert "Strategy lab · 2 rules tried" in page and "Gauge goes below 50: long BARGE for 10 days" in page and "failed development" in page
    assert "waiting for holdout" in page and "placebo 0.33%" in page


def test_the_lab_runs_daily_and_proposes_on_saturdays(conn, monkeypatch):
    called = []
    monkeypatch.setattr(lab, "run", lambda conn, llm, provider, now, proposing=False: called.append(proposing) or "Lab: ok")
    sat = float(calendar.timegm((2026, 11, 14, 7, 0, 0)))
    assert serve.run_job("lab", conn, None, None, None, None, sat) == "Lab: ok" and serve.run_job("lab", conn, None, None, None, None, sat + DAY) == "Lab: ok"
    assert called == [True, False]
