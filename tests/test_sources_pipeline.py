import time
from types import SimpleNamespace as NS
import pytest
from thal import ingest, memory, sources
from thal.__main__ import run, status
from thal.broker import PaperBroker
from thal.extract import BatchOut, EventOut
from thal.llm import LLM
from thal.prices import FakeProvider
from thal.risk import Limits
from thal.sources import Scout, safe_url
from thal.wiki import Wiki
from .conftest import DAY, T0, series
from .test_agent import Script, resp, text, use

NOW = T0 + 80 * DAY + 3600


def feed(n=3, age_days=1):
    return NS(entries=[{"title": f"story {i}", "link": f"http://x.example/{i}", "published_parsed": time.gmtime(NOW - age_days * DAY)} for i in range(n)])


def add(conn, name, status="active"):
    conn.execute("INSERT INTO sources(name, url, status, added_at) VALUES (?,?,?,?)", (name, f"https://{name.lower()}.example/rss", status, NOW))
    conn.commit()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/rss", "http://127.0.0.1/x", "http://10.0.0.5/feed", "http://169.254.169.254/latest",
                                 "ftp://a.example/x", "http://intranet/rss", "http://box.internal/rss", "http://[::1]/x", "javascript:alert(1)", ""])
def test_only_public_http_urls_can_be_fetched(url):
    with pytest.raises(ValueError): safe_url(url)


def test_public_urls_pass():
    assert safe_url(" https://www.federalregister.gov/api/v1/documents.rss ") == "https://www.federalregister.gov/api/v1/documents.rss"
    assert safe_url("http://8.8.8.8/feed") == "http://8.8.8.8/feed"


def test_seed_loads_the_starter_list_once(conn):
    n = sources.seed(conn, NOW)
    assert n >= 20 and sources.seed(conn, NOW) == 0 and len(sources.live(conn)) == n


def test_empty_feeds_are_retired_after_repeated_failures_and_recover_on_success(conn):
    add(conn, "Dead"); add(conn, "Flaky")
    fetch = lambda url: feed(0) if "dead" in url else feed(2)
    for i in range(sources.MAX_FAILS - 1):
        ingest.ingest(conn, fetch=lambda url: feed(0), now=NOW + i)
    assert [r["name"] for r in sources.live(conn)] == ["Dead", "Flaky"]
    ingest.ingest(conn, fetch=fetch, now=NOW + 10)                          # Flaky recovers, Dead hits the limit
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM sources")}
    assert rows["Dead"]["status"] == "retired" and "returned nothing" in rows["Dead"]["why"]
    assert rows["Flaky"]["status"] == "active" and rows["Flaky"]["fails"] == 0 and rows["Flaky"]["last_ok_at"] == NOW + 10
    assert [r["name"] for r in sources.live(conn)] == ["Flaky"]


def test_yield_follows_items_through_to_evidence_and_bets(conn):
    add(conn, "Good"); add(conn, "Noise")
    ingest.store(conn, [{"id": "g1", "source": "Good", "url": "u1", "title": "t", "body": "", "published_at": None},
                        {"id": "n1", "source": "Noise", "url": "u2", "title": "t", "body": "", "published_at": None},
                        {"id": "n2", "source": "Noise", "url": "u3", "title": "t", "body": "", "published_at": None}], now=NOW)
    conn.execute("INSERT INTO events(item_id, at, event_type, summary) VALUES ('g1', ?, 'other', 'e')", (NOW,))
    f = memory.open_fuse(conn, "f", "t", ["w"], now=NOW); memory.add_evidence(conn, f, 1, "supports", "n", NOW)
    conn.execute("INSERT INTO bets(created_at, source, kind, fuse_id, symbol, side) VALUES (?, 'v2', 'paper', ?, 'AAA', 'LONG')", (NOW, f))
    conn.execute("INSERT INTO marks(bet_id, horizon, ret, vs_spy, marked_at) VALUES (1, '4w', 0.05, 0.03, ?)", (NOW,)); conn.commit()
    st = {r["name"]: r for r in sources.stats(conn)}
    assert (st["Good"]["items"], st["Good"]["events"], st["Good"]["evidence"], st["Good"]["bets"]) == (1, 1, 1, 1)
    assert st["Good"]["bets_vs_spy_4w"] == pytest.approx(0.03)
    assert (st["Noise"]["items"], st["Noise"]["events"], st["Noise"]["evidence"], st["Noise"]["bets"]) == (2, 0, 0, 0)
    assert sources.stats_text(conn).splitlines()[1].startswith("Good") and "+3.0%" in sources.stats_text(conn)


def test_scout_must_test_before_adding_and_cannot_add_dead_stale_or_duplicate_feeds(conn):
    add(conn, "Existing")
    feeds = {"https://new.example/rss": feed(4), "https://dead.example/rss": feed(0), "https://old.example/rss": feed(3, age_days=400)}
    s = Scout(conn, NOW, fetch=lambda u: feeds[u])
    assert "test this exact URL" in s.add_source("New", "https://new.example/rss", "customs notices")
    assert "4 entries, newest entry 1 days old" in s.test_feed("https://new.example/rss") and "- story 0" in s.test_feed("https://new.example/rss")
    assert "no entries" in s.test_feed("https://dead.example/rss") and "stale" in s.test_feed("https://old.example/rss")
    assert "test this exact URL" in s.add_source("Dead", "https://dead.example/rss", "x") and "test this exact URL" in s.add_source("Old", "https://old.example/rss", "x")
    assert "reason are required" in s.add_source("New", "https://new.example/rss", "  ")
    assert s.add_source("New", "https://new.example/rss", "customs notices") == "added 'New' on trial"
    assert "already exists" in s.add_source("New again", "https://new.example/rss", "dup")
    assert s.call("test_feed", {"url": "http://127.0.0.1/x"}) == ("error: ValueError: only public http(s) URLs are allowed", True)
    assert s.call("drop", {})[1]
    row = conn.execute("SELECT status, added_by, why FROM sources WHERE name = 'New'").fetchone()
    assert tuple(row) == ("trial", "scout", "customs notices") and len(sources.live(conn)) == 2
    ingest.store(conn, [{"id": "e1", "source": "Existing", "url": "ue", "title": "t", "body": "", "published_at": None}], now=NOW)
    assert "not retired" in s.retire_source("Existing", "one quiet day")               # a working source gets a fair trial first
    conn.execute("UPDATE sources SET added_at = ? WHERE name = 'Existing'", (NOW - 30 * DAY,))
    ingest.store(conn, [{"id": f"e{i}", "source": "Existing", "url": f"u{i}", "title": "t", "body": "", "published_at": None} for i in range(2, 200)], now=NOW)
    assert s.retire_source("Existing", "200 items, no events") == "retired 'Existing'" and "no live source" in s.retire_source("Existing", "again")
    assert s.added == ["New"] and s.retired == ["Existing"]


def test_scout_session_end_to_end_and_adds_are_capped(conn, tmp_path):
    add(conn, "Existing")
    urls = [f"https://s{i}.example/rss" for i in range(sources.MAX_ADDS_PER_SESSION + 2)]
    turns = [resp("tool_use", *[use(i, "test_feed", url=u) for i, u in enumerate(urls)]),
             resp("tool_use", *[NS(type="tool_use", id=f"a{i}", name="add_source", input={"name": f"S{i}", "url": u, "why": "gap"}) for i, u in enumerate(urls)]),
             resp("end_turn", text("Added feeds for customs and port notices."))]
    client = Script(*turns)
    rep = sources.scout(conn, LLM(conn, client), Wiki(tmp_path / "w"), now=NOW, fetch=lambda u: feed(2))
    assert rep["finished"] and len(rep["added"]) == sources.MAX_ADDS_PER_SESSION and rep["summary"].startswith("Added feeds")
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5" and "Existing" in first["messages"][0]["content"] and "# Dashboard" in first["messages"][0]["content"]
    assert first["tools"][-1]["type"].startswith("web_search")
    assert "already added this session" in client.calls[2]["messages"][-1]["content"][-1]["content"]


def test_run_is_now_extract_then_one_analyst_session(conn, tmp_path):
    wiki = Wiki(tmp_path / "wiki")
    prov = FakeProvider({"SPY": series(100, 0.0, 81), "CANE": series(20, 0.01, 81)})
    broker = PaperBroker(conn, prov, Limits(kill_switch="/nonexistent/KILL"), starting_cash=100_000)
    add(conn, "F")

    class Both(Script):          # one parse-style reply for extract, then scripted analyst turns
        pass
    ex = NS(parsed_output=BatchOut(events=[EventOut(item=0, event_type="weather_climate", summary="India monsoon ended 18% below normal.", magnitude=4, novelty=4, reliability=4)]),
            content=[], stop_reason="end_turn", model="claude-haiku-5-5", usage=NS(input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    client = Both(ex,
                  resp("tool_use", use(1, "open_fuse", title="India sugar shortfall", thesis="India must import.", watch=["mill output"], event_ids=[1]),
                       use(2, "propose_bet", symbol="CANE", side="LONG", thesis="t", invalidation="i", horizon_days=30, conviction=0, tags=["weather"]),
                       use(3, "propose_bet", symbol="CANE", side="LONG", thesis="t", invalidation="i", horizon_days=30, conviction=5, fuse_id=1),
                       use(4, "note_for_page", page="Lessons", kind="rule", note="x")),
                  resp("end_turn", text("One fuse, one shadow bet, one real bet.")))
    rep = run(conn, LLM(conn, client), wiki, prov, broker, now=NOW, fetch=lambda u: feed(1), fetcher=lambda u: ("full article", "http"))
    assert rep["new_items"] == 1 and rep["extract"]["events"] == 1 and rep["analyst"]["finished"]
    kinds = [tuple(r) for r in conn.execute("SELECT kind, fuse_id FROM bets ORDER BY id")]
    assert kinds == [("shadow", None), ("paper", 1)]                          # the shadow bet is scored but places no order
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1 and rep["analyst"]["notes"] == 0
    assert conn.execute("SELECT status FROM fuses").fetchone()[0] == "fired"
    assert [c["model"] for c in client.calls] == ["claude-haiku-5-5", "claude-fable-5-2", "claude-fable-5-2"]
    again = run(conn, LLM(conn, Script()), wiki, prov, broker, now=NOW + 60, fetch=lambda u: feed(1), fetcher=lambda u: ("x", "http"))
    assert again["new_items"] == 0 and again["analyst"] is None               # nothing new: no model calls at all
    assert "India sugar shortfall" not in status(conn, LLM(conn, Script()), wiki, broker).split("## Open fuses")[1].split("## Wiki")[0]
