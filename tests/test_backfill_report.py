import calendar
import json
import pytest
from thal import backfill, extract, fetch, forecasts, ledger, report, serve, sources
from thal.extract import BatchOut, EventOut
from thal.llm import LLM
from thal.prices import FakeProvider
from .conftest import DAY, T0, series
from .fakes import FakeClient


def ev(i):
    return EventOut(item=i, event_type="trade_flow", summary=f"event {i}", magnitude=3, novelty=3, reliability=3)


def scan(folder, name, heads, **extra):
    (folder / name).write_text(json.dumps({"timestamp": name[:15], "headlines": heads, **extra}))


H = lambda n, src="gCaptain": {"source": src, "title": f"Port story {n}", "link": f"https://x.test/{n}?utm_source=rss", "summary": f"<p>Summary {n}</p>"}


def test_load_dates_items_by_first_scan_and_is_safe_to_repeat(conn, tmp_path):
    scan(tmp_path, "20260303_072341.json", [H(1), H(2), {"source": "S", "title": "", "link": "u"}])
    scan(tmp_path, "20260410_120000.json", [H(2), H(3, "Mining.com")])
    (tmp_path / "20260602_203137_result.json").write_text(json.dumps({"events_analyzed": 3}))      # not a scan
    (tmp_path / "broken.json").write_text("{nope")
    rep = backfill.load(conn, tmp_path)
    assert rep == {"files": 2, "skipped_files": 2, "headlines": 4, "new": 3}
    assert backfill.load(conn, tmp_path)["new"] == 0
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM items")}
    march = float(calendar.timegm((2026, 3, 3, 7, 23, 41)))
    assert rows["Port story 2"]["published_at"] == march and rows["Port story 2"]["fetched_at"] == march    # first sighting wins
    assert rows["Port story 3"]["source"] == "v1: Mining.com" and rows["Port story 1"]["body"] == "Summary 1"
    assert backfill.pending(conn) == 3


def test_backfilled_items_are_never_fetched_or_taken_by_the_live_intake(conn, tmp_path):
    scan(tmp_path, "20260303_072341.json", [H(1), H(2)])
    backfill.load(conn, tmp_path)
    called = []
    fetch.fetch_pending(conn, fetcher=lambda u: called.append(u) or ("text " * 200, "http"), sleep=lambda s: None)
    client = FakeClient()
    assert extract.extract_pending(conn, LLM(conn, client), now=T0)["items"] == 0
    assert called == [] and client.calls == [] and backfill.pending(conn) == 2


def test_backfill_events_keep_their_date_and_are_not_new_to_the_analyst(conn, tmp_path):
    scan(tmp_path, "20260303_072341.json", [H(i) for i in range(25)])
    backfill.load(conn, tmp_path)
    sources.seed(conn, T0)
    client = FakeClient(BatchOut(events=[ev(0), ev(3)]), RuntimeError("overloaded"))
    seen = []
    d = backfill.extract_backfill(conn, LLM(conn, client), now=T0, progress=lambda done, n: seen.append(n))
    assert d["items"] == 20 and d["events"] == 2 and d["failed_batches"] == 1 and backfill.pending(conn) == 5   # the failed batch waits
    evs = conn.execute("SELECT * FROM events").fetchall()
    assert all(e["connected"] == 1 and e["at"] == float(calendar.timegm((2026, 3, 3, 7, 23, 41))) for e in evs)
    assert conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'event'").fetchone()[0] == 2    # searchable
    assert all(r["items"] == 0 for r in sources.stats(conn))               # live sources' yield statistics are untouched
    assert seen == [25]


def test_shards_split_the_work_without_overlap(conn, tmp_path):
    scan(tmp_path, "20260303_072341.json", [H(i) for i in range(10)])
    backfill.load(conn, tmp_path)
    a, b = FakeClient(BatchOut(events=[])), FakeClient(BatchOut(events=[]))
    da = backfill.extract_backfill(conn, LLM(conn, a), shard=(0, 2), now=T0)
    db_ = backfill.extract_backfill(conn, LLM(conn, b), shard=(1, 2), now=T0)
    assert da["items"] == 5 and db_["items"] == 5 and backfill.pending(conn) == 0


def test_live_extraction_still_stores_events_as_unseen(conn):
    from thal import ingest
    ingest.store(conn, [{"id": "a", "source": "S", "url": "u", "title": "t", "body": "", "published_at": None}], now=T0)
    extract.extract_pending(conn, LLM(conn, FakeClient(BatchOut(events=[ev(0)]))), now=T0)
    assert conn.execute("SELECT connected FROM events").fetchone()[0] == 0


def test_scorecard_with_nothing_scored_yet(conn):
    text = report.scorecard(conn, T0)
    assert "Weekly scorecard" in text and "None resolved yet; 0 open." in text and "0 recorded, none old enough" in text
    assert "$0.00 spent" in text


def test_scorecard_reports_forecast_skill_and_bets_against_sector(conn):
    forecasts.ensure(conn)
    for prob, base, outcome, kind, tag in ((0.9, 0.5, 1, "price", "energy"), (0.2, 0.5, 0, "price", "energy"), (0.7, 0.6, 0, "market", "macro")):
        conn.execute("INSERT INTO forecasts(created_at, kind, ref, question, due_at, prob, baseline, tags, resolved_at, outcome)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)", (T0, kind, "X", "q", T0 + DAY, prob, base, json.dumps([tag]), T0 + 2 * DAY, outcome))
    conn.execute("INSERT INTO forecasts(created_at, kind, ref, question, due_at, prob, baseline) VALUES (?,?,?,?,?,?,?)", (T0, "price", "X", "q", T0 + 9 * DAY, 0.5, 0.5))
    prov = FakeProvider({"SPY": series(100, 0.0, 40), "XLE": series(50, 0.001, 40), "UP": series(10, 0.01, 40), "DOWN": series(10, -0.01, 40)})
    for sym, side in (("UP", "LONG"), ("DOWN", "LONG"), ("DOWN", "SHORT"), ("UP", "SHORT")):
        ledger.add_bet(conn, symbol=sym, side=side, created_at=T0 - 3600, benchmark="XLE", kind="shadow")
    ledger.add_bet(conn, symbol="UP", side="LONG", source="v1", created_at=T0 - 3600, kind="alert")       # the old bot is not in this report
    now = T0 + 41 * DAY
    ledger.mark_all(conn, prov, now)
    text = report.scorecard(conn, T0 + 3 * DAY)
    assert "all: 3 resolved, skill" in text and "(too few to judge)" in text and "1 resolved this week" not in text
    assert "3 resolved this week, 1 open" in text and "- price: 2 resolved, skill +9" in text and "- market: 1 resolved, skill -3" in text
    assert "By topic: energy" in text
    assert "4 recorded." in text and "- 1w: n=4" in text and "- 4w: n=4" in text and "vs sector" in text
    assert "To date by side: LONG" in text and "Best: long UP" in text and "Worst: short UP" in text
    assert len(text) < 1900                                               # fits in one Discord message


def at(day, hour, minute=0):
    return float(calendar.timegm((2026, 11, day, hour, minute, 0)))


def test_report_is_due_on_sunday_afternoon_once(conn):
    for job in ("dream", "scout", "analyst", "intake"):
        serve.mark_run(conn, job, at(15, 12, 5))
    assert serve.due(conn, at(15, 12, 30)) == [] and serve.due(conn, at(15, 13, 0)) == ["report"]
    serve.mark_run(conn, "report", at(15, 13, 0))
    assert serve.due(conn, at(15, 14)) == []
    for job in ("dream", "analyst", "intake"):
        serve.mark_run(conn, job, at(16, 12, 5))
    assert serve.due(conn, at(16, 13, 30)) == []                           # Monday: no report


def test_report_job_returns_the_scorecard(conn):
    class B:
        def settle(self, now): return 0
    out = serve.run_job("report", conn, None, None, FakeProvider({}), B(), T0)
    assert out.startswith("**Weekly scorecard")
