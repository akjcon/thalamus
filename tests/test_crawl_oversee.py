import calendar
from types import SimpleNamespace as NS
from thal import crawl, oversee, serve, sources
from thal.crawl import Candidate, Judged, Proposed, Verdict
from thal.llm import LLM
from thal.oversee import Review
from .fakes import FakeClient

NOW = float(calendar.timegm((2026, 11, 17, 6, 0, 0)))
HOUR, DAY = 3600.0, 86400.0


def feed(n, age_days=1):
    stamp = __import__("time").gmtime(NOW - age_days * DAY)
    return NS(entries=[{"title": f"Port closed {i}", "link": f"http://x/{i}", "summary": "s", "published_parsed": stamp} for i in range(n)])


def test_crawl_proposes_tests_with_code_and_adds_only_what_the_judge_keeps(conn):
    conn.execute("INSERT INTO sources(name, url, status, added_at) VALUES ('gCaptain', 'https://gcaptain.com/feed/', 'active', 1)")
    proposed = Proposed(candidates=[
        Candidate(name="Port Authority", url="https://port.example/news", kind="feed", why="closures"),      # feed is found on its page
        Candidate(name="Direct", url="https://direct.example/rss.xml", kind="feed", why="is a feed"),
        Candidate(name="Stale", url="https://stale.example/feed", kind="feed", why="old"),
        Candidate(name="Gone", url="https://gone.example/", kind="feed", why="dead"),
        Candidate(name="gCaptain again", url="https://www.gcaptain.com/", kind="feed", why="duplicate"),     # already read
        Candidate(name="Local", url="http://127.0.0.1/feed", kind="feed", why="not public"),
        Candidate(name="Canal draft table", url="https://canal.example/drafts", kind="data", why="daily draft limit"),
        Candidate(name="Vessel tracking", url="https://ais.example/", kind="paid", why="about $500 a month")])
    judged = Judged(verdicts=[Verdict(url="https://port.example/news", keep=True, name="Port Authority Notices", why="first-hand closures"),
                              Verdict(url="https://direct.example/rss.xml", keep=False, why="marketing")])
    client = FakeClient(proposed, judged)
    def fetch(url):
        if url in ("https://port.example/notices.xml", "https://direct.example/rss.xml"): return feed(3)
        if url == "https://stale.example/feed": return feed(3, age_days=90)
        return feed(0)
    def get(url):
        if "gone" in url: raise OSError("refused")
        return '<html><head><link rel="alternate" type="application/rss+xml" href="/notices.xml"></head></html>' if "port.example" in url else "<html></html>"
    rep = crawl.crawl(conn, LLM(conn, client), NOW, fetch=fetch, get=get)
    assert rep == {"proposed": 6, "working": 2, "added": ["Port Authority Notices"], "new_proposals": 2}
    row = conn.execute("SELECT url, status, added_by, kind FROM sources WHERE name = 'Port Authority Notices'").fetchone()
    assert tuple(row) == ("https://port.example/notices.xml", "trial", "crawl", "rss")
    status = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM candidates")}
    assert status == {"Port Authority": "added", "Direct": "rejected", "Stale": "dead", "Gone": "dead",
                      "Canal draft table": "proposal", "Vessel tracking": "proposal"}
    assert {p["name"] for p in crawl.proposals(conn)} == {"Canal draft table", "Vessel tracking"}
    assert "gcaptain.com" in client.calls[0]["messages"][0]["content"]            # told what is already read
    assert "Port closed 0" in client.calls[1]["messages"][0]["content"] and client.calls[0]["model"] == "claude-haiku-5-5"
    # the next run does not propose or fetch the same sites again
    client.replies += [Proposed(candidates=[Candidate(name="Port again", url="https://port.example/other", kind="feed", why="x")])]
    assert crawl.crawl(conn, LLM(conn, client), NOW + DAY, fetch=fetch, get=get)["proposed"] == 0 and len(client.calls) == 3


def test_overseer_reports_stopped_jobs_even_without_the_model_and_directs_the_crawler(conn):
    serve.mark_run(conn, "intake", NOW - 9 * HOUR); serve.mark_run(conn, "analyst", NOW - 10 * HOUR)
    conn.execute("INSERT INTO job_log(at, job, ok, summary) VALUES (?, 'dream', 0, 'Dream failed: RuntimeError: disk full')", (NOW - HOUR,))
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://ais.example/', 'Vessel tracking', 1, 'proposal', '[paid] about $500 a month')")
    assert oversee.stopped(conn, NOW) == ["intake has not run for 9 hours (expected within 6)", "dream failed on its last run: Dream failed: RuntimeError: disk full"]
    text = oversee.health(conn, NOW)
    assert "Vessel tracking" in text and "intake has not run for 9 hours" in text and "## Scorecard" in text
    client = FakeClient(Review(verdict="Intake has stalled.", problems=["intake stopped"], crawl_directions="Rhine and Danube port notices",
                               for_owner=["Vessel tracking at about $500 a month"]), RuntimeError("api down"))
    msg = oversee.review(conn, LLM(conn, client), NOW)
    assert msg.startswith("**Overseer** STOPPED OR FAILING: intake has not run") and "For you to decide:" in msg and "Vessel tracking" in msg
    assert client.calls[0]["model"] == "claude-fable-5-2" and crawl.directions(conn) == "Rhine and Danube port notices"
    msg = oversee.review(conn, LLM(conn, client), NOW)                         # the model cannot be reached
    assert msg.startswith("**Overseer** STOPPED OR FAILING") and "No review this time" in msg
    # the crawler is shown those directions on its next run
    client.replies += [Proposed(candidates=[])]
    crawl.propose(conn, LLM(conn, client), NOW)
    assert "Rhine and Danube port notices" in client.calls[-1]["messages"][0]["content"]


def test_a_healthy_system_says_so(conn):
    for job in oversee.EXPECTED:
        serve.mark_run(conn, job, NOW - HOUR)
    assert oversee.stopped(conn, NOW) == []
    msg = oversee.review(conn, LLM(conn, FakeClient(Review(verdict="All parts are running and on focus."))), NOW)
    assert msg == "**Overseer** All jobs are running.\nAll parts are running and on focus."
