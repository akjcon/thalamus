"""The strategy lab: rules are data, code tests them, and the gates (a development bar that rises with every rule
tried, one run on a holdout the proposer never sees, then a live shadow record) are enforced in code."""
import calendar
import json
from datetime import date, timedelta
import pytest
from thal import lab, serve, ui
from thal.lab import Proposed, Rule
from thal.llm import LLM
from thal.prices import FakeProvider
from .fakes import FakeClient

DAY = 86400.0
START, END = date(2015, 1, 1), date(2026, 11, 10)
NOW = float(calendar.timegm((2026, 11, 12, 7, 0, 0)))
ts = lambda d: float(calendar.timegm(d.timetuple())) + 14.5 * 3600          # that day's session open


def world(conn, every=40, low_for=5):
    """A gauge that sits at 100 and drops to 40 for a few days every `every` days, and a stock that rises for ten
    days starting three days after each drop (by 1% a day, or 0.5% on every other drop). The market is flat."""
    n = (END - START).days + 1
    days = [START + timedelta(days=i) for i in range(n)]
    conn.executemany("INSERT INTO readings VALUES ('Gauge', ?, ?, ?, 'cm')", [(d.isoformat(), ts(d), 40.0 if i % every < low_for and i >= every else 100.0) for i, d in enumerate(days)])
    conn.execute("INSERT INTO sources(name, url, added_at, kind, why) VALUES ('Gauge', 'https://g.example', 1, 'series', 'barges load light below 50 cm')")
    conn.commit()
    px, bars = 100.0, []
    for i, d in enumerate(days):
        k, phase = divmod(i, every)
        if k >= 1 and 3 <= phase <= 12:
            px *= 1.01 if k % 2 else 1.005
        bars.append((ts(d), px))
    flat = [(ts(d), 50.0) for d in days]
    data = {"BARGE": bars, "SPY": flat}
    return lambda sym: data.get(sym, [])


def rule(**kw):
    return Rule(**{**dict(name="Low water lifts barge rates", hypothesis="Below 50 cm barges load light, so rates rise for weeks.", series="Gauge",
                          stat="level", op="below", value=50, symbol="barge", side="LONG", hold_days=10), **kw})


def test_a_rule_is_tested_only_on_what_was_known_and_entered_after_a_lag(conn):
    prices = world(conn)
    dev, held = lab.evaluate(conn, rule(), prices)
    first = dev[0]
    assert first["signal"] == "2015-02-10" and first["exit"] == "2015-02-22"          # fired on day 40; in at the close two days later, out ten days on
    assert first["excess"] == pytest.approx(1.01 ** 10 - 1 - lab.COST)                # in before the rise starts, so all ten days of it are caught
    assert all(t["exit"] < lab.HOLDOUT_START for t in dev) and all(t["signal"] >= lab.HOLDOUT_START for t in held)
    assert len(dev) == 104 and len(held) == 3                                         # one drop every 40 days; the trade across 1 July counts for neither
    s = lab.summary(dev)
    assert s["n"] == 104 and s["hit"] == 1.0 and s["t"] > 10
    assert lab.summary([]) == {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0}


def test_statistics_use_only_the_past(conn):
    world(conn)
    rows = lab.readings(conn, "Gauge")
    change = dict(lab.stat_series(rule(stat="change", window=1), rows))
    assert change[date(2015, 2, 10)] == -60 and change[date(2015, 2, 15)] == 60 and date(2015, 1, 1) not in change
    total = dict(lab.stat_series(rule(stat="sum", window=10), rows))
    assert total[date(2015, 2, 14)] == 5 * 40 + 5 * 100 and date(2015, 1, 5) not in total      # too few days for a 10-day total
    normal = lab.stat_series(rule(versus="normal"), rows)
    assert normal[0][0].year == 2020 and all(abs(x) <= 60 for _, x in normal)                  # needs five earlier years of the same date
    assert lab.crossings(rule(), [(date(2020, 1, 1), 60), (date(2020, 1, 2), 40), (date(2020, 1, 3), 30), (date(2020, 1, 4), 70), (date(2020, 1, 5), 45)]) \
        == [date(2020, 1, 2), date(2020, 1, 5)]                                                # the day it becomes true, not every day it stays true


def test_the_bar_rises_with_every_rule_tried_and_each_rule_is_tried_once(conn):
    prices = world(conn)
    assert lab.bar(1) == 3.0 and lab.bar(90) == 3.0 and lab.bar(100) == 3.03 and lab.bar(1000) == 3.72
    good = lab.record(conn, rule(), prices, NOW)
    assert good["status"] == "passed_dev" and good["n"] == 104 and good["bar"] == 3.0
    assert lab.record(conn, rule(name="Same rule, new name", hypothesis="x"), prices, NOW) == {"name": "Same rule, new name", "status": "duplicate"}
    never = lab.record(conn, rule(name="Never fires", op="above", value=500), prices, NOW)
    assert never["status"] == "failed_dev" and never["n"] == 0
    wrong = lab.record(conn, rule(name="Wrong way", side="SHORT"), prices, NOW)
    assert wrong["status"] == "failed_dev" and wrong["mean"] < 0
    for bad in (rule(name="No series", series="Volga"), rule(name="No prices", symbol="NOPE"), rule(name="Too long", hold_days=400),
                rule(name="Bad benchmark", benchmark="NOPE"), rule(name="Against itself", benchmark="Barge")):
        assert lab.record(conn, bad, prices, NOW)["status"] == "invalid"
    for same in (rule(symbol="BARGE"), rule(benchmark="spy"), rule(window=7)):         # one rule, however it is spelled
        assert lab.record(conn, same, prices, NOW)["status"] == "duplicate"
    def down(sym): raise lab.Unavailable("prices down")
    before = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert lab.record(conn, rule(name="Unlucky", hold_days=11), down, NOW)["status"] == "postponed"     # an outage settles nothing
    assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == before and lab.holdout(conn, down, NOW) == []
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM strategies")}
    assert rows["Never fires"]["note"] == "only 0 trades" and "at least two years" in rows["No series"]["note"]
    conn.executemany("INSERT INTO strategies(created_at, key, name, hypothesis, spec, status) VALUES (1, ?, 'x', 'h', '{}', 'failed_dev')", [(f"k{i}",) for i in range(200)])
    late = lab.record(conn, rule(name="Late, with a short hold", hold_days=9), prices, NOW)
    assert late["bar"] == lab.bar(204) == 3.26 and late["status"] == "passed_dev"       # 3 tried + 200 + this one; invalid ones do not count


def test_holdout_is_run_once_and_then_the_rule_trades_live_as_shadow_bets(conn):
    prices = world(conn)
    lab.record(conn, rule(), prices, NOW)
    lab.record(conn, rule(name="Loses later", symbol="FADE"), lambda s: prices("BARGE" if s == "FADE" else s), NOW)
    falls = lambda s: [(t, 100.0 - i * 0.001) for i, (t, _) in enumerate(prices("SPY"))] if s == "FADE" else prices(s)
    done = {d["name"]: d for d in lab.holdout(conn, falls, NOW)}
    assert done["Low water lifts barge rates"]["status"] == "live" and done["Low water lifts barge rates"]["n"] == 3
    assert done["Loses later"]["status"] == "failed_holdout"
    assert lab.holdout(conn, falls, NOW) == []                                          # there is no second run
    assert conn.execute("SELECT holdout_at FROM strategies WHERE name = 'Loses later'").fetchone()[0] == NOW

    seen = lab.registry(conn)
    assert seen.count("passed development") == 2 and "holdout" not in seen and "live" not in seen       # the proposer cannot tell the two apart

    provider = FakeProvider({"BARGE": prices("BARGE"), "SPY": prices("SPY")})
    assert lab.run_live(conn, provider, NOW) == []                                      # last drop was 19 days ago: too old to act on
    d = date(2026, 11, 10)
    conn.execute("UPDATE readings SET value = 40 WHERE source = 'Gauge' AND day = ?", (d.isoformat(),)); conn.commit()
    assert lab.run_live(conn, FakeProvider({}), NOW) == ["Low water lifts barge rates: LONG barge (not recorded: no recent price; not a tradeable instrument)"]
    assert lab.run_live(conn, provider, NOW) == ["Low water lifts barge rates: LONG barge"]         # the signal was not used up by the failure
    bet = conn.execute("SELECT * FROM bets").fetchone()
    assert (bet["source"], bet["kind"], bet["symbol"], bet["horizon_days"], bet["benchmark"]) == ("lab", "shadow", "BARGE", 10, "SPY")
    assert json.loads(bet["tags"]) == ["lab", "lab-1"] and "Lab rule #1" in bet["thesis"] and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert lab.run_live(conn, provider, NOW + 3600) == []                               # the same signal is acted on once
    from thal import ledger
    assert ledger.open_bets(conn) == []                                                 # not among the analyst's bets to review

    # eight closed live bets that lost: the rule is retired
    for i in range(8):
        conn.execute("INSERT INTO bets(created_at, source, kind, symbol, side, tags, closed_at) VALUES (?, 'lab', 'shadow', 'BARGE', 'LONG', '[\"lab\", \"lab-1\"]', ?)", (NOW, NOW))
        conn.execute("INSERT INTO marks(bet_id, horizon, ret, vs_bench, marked_at) VALUES (?, 'now', -0.02, -0.03, ?)", (conn.execute("SELECT MAX(id) FROM bets").fetchone()[0], NOW))
    conn.commit()
    assert lab.live_record(conn, 1) == {"n": 8, "mean": -0.03}
    lab.run_live(conn, provider, NOW + DAY)
    assert [tuple(r) for r in conn.execute("SELECT status, note FROM strategies WHERE id = 1")] == [("retired", "retired: -3.0% against its benchmark over 8 live bets")]


def test_the_proposer_sees_development_figures_only_and_the_job_reports(conn):
    prices = world(conn)
    conn.execute("INSERT INTO readings VALUES ('kalshi:KXRAIN-4', '2020-01-01', 1, 0.3, 'prob')")
    conn.execute("INSERT INTO readings VALUES ('New gauge', '2026-01-01', 1, 5, 'm')"); conn.commit()
    listed = lab.catalog(conn)
    assert listed.startswith("- Gauge (cm; a level): daily since 2015-01-01") and "barges load light below 50 cm" in listed
    assert "kalshi" not in listed and "New gauge" not in listed                          # too short a record to test anything on
    client = FakeClient(Proposed(rules=[rule(), rule(name="Never fires", op="above", value=500)]))
    out = lab.run(conn, LLM(conn, client), FakeProvider({}), NOW, proposing=True, prices=prices)
    assert out == ("Lab: 2 rules proposed, 1 passed development (Low water lifts barge rates). Holdout: Low water lifts barge rates passed. "
                   "1 live, 0 waiting for holdout trades, 2 tried in all.")
    sent = client.calls[0]
    assert sent["model"] == "claude-fable-5-2" and "the next must reach t 3.0" in sent["messages"][0]["content"] and "(none yet)" in sent["messages"][0]["content"]
    assert "2026-07-01" in sent["system"][0]["text"]
    assert lab.run(conn, LLM(conn, FakeClient()), FakeProvider({}), NOW + DAY, prices=prices) == "Lab: 1 live, 0 waiting for holdout trades, 2 tried in all."   # no model call on other days

    page = ui.page(conn, NOW)
    assert "Strategy lab · 2 rules tried" in page and "Gauge goes below 50: long BARGE for 10 days" in page and "failed development" in page
    assert ">live<" in page and "n=104" in page and "n=3" in page


def test_the_lab_runs_daily_and_proposes_on_saturdays(conn, monkeypatch):
    called = []
    monkeypatch.setattr(lab, "run", lambda conn, llm, provider, now, proposing=False: called.append(proposing) or "Lab: ok")
    sat = float(calendar.timegm((2026, 11, 14, 7, 0, 0)))
    assert serve.run_job("lab", conn, None, None, None, None, sat) == "Lab: ok" and serve.run_job("lab", conn, None, None, None, None, sat + DAY) == "Lab: ok"
    assert called == [True, False]
