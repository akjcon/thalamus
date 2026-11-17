import calendar
from types import SimpleNamespace as NS
from thal import crawl, oversee, serve, sources
from thal import ingest
from thal.crawl import Built, Candidate, Judged, Proposed, Verdict
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
    built = Built(decision="series", name="Canal draft limit", recipe_json='{"format": "regex", "pattern": "draft today: ([0-9.]+) m", "unit": "m"}',
                  note="Maximum draft at the locks; below 13 m large ships must load light.")
    client = FakeClient(proposed, judged, built)
    def fetch(url):
        if url in ("https://port.example/notices.xml", "https://direct.example/rss.xml"): return feed(3)
        if url == "https://stale.example/feed": return feed(3, age_days=90)
        return feed(0)
    def get(url):
        if "gone" in url: raise OSError("refused")
        if "canal.example" in url: return "<html><body><p>Maximum authorised draft today: 13.41 m for the new locks</p></body></html>"
        return '<html><head><link rel="alternate" type="application/rss+xml" href="/notices.xml"></head></html>' if "port.example" in url else "<html></html>"
    rep = crawl.crawl(conn, LLM(conn, client), NOW, fetch=fetch, get=get)
    assert rep == {"proposed": 6, "working": 2, "added": ["Port Authority Notices"], "built": ["Canal draft limit"], "needs_work": 0, "new_proposals": 1}
    canal = conn.execute("SELECT * FROM sources WHERE name = 'Canal draft limit'").fetchone()
    assert (canal["kind"], canal["status"], canal["added_by"], canal["url"]) == ("series", "trial", "build", "https://canal.example/drafts")
    # intake then reads it with the recipe the builder wrote
    assert ingest.ingest(conn, [dict(canal)], get=get, now=NOW)["Canal draft limit"] == 1
    assert conn.execute("SELECT value, unit FROM readings").fetchone()[:] == (13.41, "m")
    assert "13.41 m on 2026-11-17" in conn.execute("SELECT title FROM items").fetchone()[0]
    row = conn.execute("SELECT url, status, added_by, kind FROM sources WHERE name = 'Port Authority Notices'").fetchone()
    assert tuple(row) == ("https://port.example/notices.xml", "trial", "crawl", "rss")
    status = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM candidates")}
    assert status == {"Port Authority": "added", "Direct": "rejected", "Stale": "dead", "Gone": "dead",
                      "Canal draft table": "added", "Vessel tracking": "proposal"}
    assert [p["name"] for p in crawl.proposals(conn)] == ["Vessel tracking"]                 # paid: only the owner can decide
    assert "gcaptain.com" in client.calls[0]["messages"][0]["content"]            # told what is already read
    assert "Port closed 0" in client.calls[1]["messages"][0]["content"] and client.calls[0]["model"] == "claude-haiku-5-5"
    # the next run does not propose or fetch the same sites again
    client.replies += [Proposed(candidates=[Candidate(name="Port again", url="https://port.example/other", kind="feed", why="x")])]
    assert crawl.crawl(conn, LLM(conn, client), NOW + DAY, fetch=fetch, get=get)["proposed"] == 0 and len(client.calls) == 4


def test_overseer_reports_stopped_jobs_even_without_the_model_and_directs_the_crawler(conn):
    serve.mark_run(conn, "intake", NOW - 9 * HOUR); serve.mark_run(conn, "analyst", NOW - 10 * HOUR)
    conn.execute("INSERT INTO job_log(at, job, ok, summary) VALUES (?, 'dream', 0, 'Dream failed: RuntimeError: disk full')", (NOW - HOUR,))
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://ais.example/', 'Vessel tracking', 1, 'proposal', '[paid] about $500 a month')")
    assert oversee.stopped(conn, NOW) == ["intake has not run for 9 hours (expected within 6)", "dream failed on its last run: Dream failed: RuntimeError: disk full"]
    conn.execute("INSERT INTO items(id, source, title, fetched_at, extracted) VALUES ('k', 'Port News', 'Strait closed', ?, 1), ('d', 'Daily Wire', 'Celebrity wedding', ?, 2)", (NOW - HOUR, NOW - HOUR))
    conn.execute("INSERT INTO events(item_id, at, event_type, summary) VALUES ('k', ?, 'other', 'The strait closed to tankers.')", (NOW,))
    conn.execute("INSERT INTO strategies(created_at, key, name, hypothesis, spec, status) VALUES (1, 'k', 'n', 'h', '{}', 'failed_dev')")
    text = oversee.health(conn, NOW)
    assert "Vessel tracking" in text and "intake has not run for 9 hours" in text and "## Scorecard" in text
    assert "- kept (Port News): The strait closed to tankers." in text and "- dropped (Daily Wire): Celebrity wedding" in text   # a sample to check the cheap model on
    assert "- rules failed_dev: 1" in text and "prediction-market prices stored 0" in text
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


def test_builder_gets_one_correction_and_what_it_cannot_read_is_kept_with_the_reason(conn):
    for name, url in (("Lock queue", "https://locks.example/api"), ("Walled", "https://walled.example/"), ("Scripted", "https://js.example/")):
        conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES (?, ?, 1, 'proposal', '[data] why')", (url, name))
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://ais.example/', 'Paid', 1, 'proposal', '[paid] $500')")
    api = '{"data": [{"t": "2026-11-16T06:00:00Z", "waiting": "41"}, {"t": "2026-11-17T06:00:00Z", "waiting": "57"}]}'
    def get(url):
        if "walled" in url: raise PermissionError("http 403")
        return api if "locks" in url else "<html><body><script>app()</script></body></html>"
    client = FakeClient(Built(decision="series", recipe_json='{"format": "json", "rows": "data", "time": "t", "value": "queue", "unit": "ships"}'),
                        Built(decision="series", name="Lock queue length", recipe_json='{"format": "json", "rows": "data", "time": "t", "value": "waiting", "unit": "ships"}', note="Ships waiting."),
                        Built(decision="skip", note="needs a browser: the page is built by scripts"))
    out = crawl.build(conn, LLM(conn, client), get, NOW)
    assert out == {"built": ["Lock queue length"], "needs_work": 2} and len(client.calls) == 3          # the paid one and the walled one cost no model call
    assert "rejected by code: the recipe found no readings" in client.calls[1]["messages"][0]["content"]
    assert client.calls[0]["model"] == "claude-opus-5-5"
    notes = {r["name"]: (r["status"], r["note"]) for r in conn.execute("SELECT * FROM candidates")}
    assert notes["Walled"][0] == "needs_work" and "could not be fetched (PermissionError)" in notes["Walled"][1]
    assert notes["Scripted"][0] == "needs_work" and "needs a browser" in notes["Scripted"][1] and notes["Paid"][0] == "proposal"
    src = dict(conn.execute("SELECT * FROM sources").fetchone())
    ingest.ingest(conn, [src], get=get, now=NOW)
    assert [tuple(r) for r in conn.execute("SELECT day, value FROM readings ORDER BY day")] == [("2026-11-16", 41.0), ("2026-11-17", 57.0)]


def test_a_watched_page_reports_only_what_is_new(conn):
    feed = {"name": "Strait notices", "url": "https://strait.example/notices", "kind": "page", "recipe": '{"after": "Current notices", "before": "Contact us"}'}
    page = lambda *notes: ("<html><body><div>Home | About | A very long navigation menu line here</div><h2>Current notices</h2>"
                           + "".join(f"<p>{n}</p>" for n in notes) + "<div>Contact us at the harbour office, open daily</div></body></html>")
    first = page("Notice 41: northbound traffic suspended for fog since 04:00")
    assert ingest.ingest(conn, [feed], get=lambda u: first, now=NOW) == {"Strait notices": 0}            # the first visit only remembers
    assert ingest.ingest(conn, [feed], get=lambda u: first, now=NOW + 3600) == {"Strait notices": 0}
    second = page("Notice 42: strait closed to tankers over 250 m until further notice", "Notice 41: northbound traffic suspended for fog since 04:00")
    assert ingest.ingest(conn, [feed], get=lambda u: second, now=NOW + 7200) == {"Strait notices": 1}
    item = conn.execute("SELECT title, body FROM items").fetchone()
    assert item["title"] == "Strait notices: Notice 42: strait closed to tankers over 250 m until further notice"
    assert "Notice 41" not in item["body"] and "navigation" not in item["body"] and "Contact us" not in item["body"]
    assert ingest.ingest(conn, [feed], get=lambda u: first, now=NOW + 9000) == {"Strait notices": 0}     # an old notice coming back is not news


def test_an_unchanged_page_is_not_counted_as_a_failing_source(conn):
    conn.execute("INSERT INTO sources(name, url, status, added_at, kind) VALUES ('Quiet page', 'https://q.example/', 'trial', 1, 'page')")
    html = "<html><body><p>Nothing to report at this terminal today, operations normal</p><p>Second line of ordinary text on the page</p></body></html>"
    for i in range(sources.MAX_FAILS + 2):
        ingest.ingest(conn, get=lambda u: html, fetch=lambda u: feed(1), now=NOW + i)
    assert conn.execute("SELECT status, fails FROM sources WHERE name = 'Quiet page'").fetchone()[:] == ("trial", 0)


def test_recipes_read_csv_and_reject_what_does_not_work():
    csv = "date,station,rain_mm\n2026-11-15,Gatun,12.5\n2026-11-16,Gatun,-9999\n2026-11-17,Gatun,3.0\n"
    recipe = {"format": "csv", "time": "date", "value": "rain_mm", "unit": "mm", "min": 0}
    assert [v for _, v in ingest.recipe_readings(recipe, csv, NOW)] == [12.5, 3.0]                       # the missing-data marker is dropped
    assert ingest.check_recipe("series", recipe, csv, NOW) == ""
    assert "no readings" in ingest.check_recipe("series", {**recipe, "value": "rainfall"}, csv, NOW)
    assert "must state its unit" in ingest.check_recipe("series", {**recipe, "unit": ""}, csv, NOW)
    assert "not recent" in ingest.check_recipe("series", recipe, csv, NOW + 400 * DAY)
    assert "pattern of at most" in ingest.check_recipe("series", {"format": "regex", "pattern": "a" * 400, "unit": "m"}, "x", NOW)
    assert "only 0 lines" in ingest.check_recipe("page", {}, "<html><script>x()</script></html>", NOW)


def test_builder_follows_its_own_lead_to_the_address_that_holds_the_data(conn):
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://water.example/', 'River gauges', 1, 'proposal', '[data] stage levels')")
    def get(url):
        if url == "https://water.example/api/stage.csv": return "time,stage_ft\n2026-11-16 12:00,4.2\n2026-11-17 12:00,3.1\n"
        return "<html><body><p>Welcome to the water data portal. Use our CSV service for stage data.</p></body></html>"
    client = FakeClient(Built(decision="elsewhere", try_url="https://water.example/api/stage.csv"),
                        Built(decision="series", name="River stage at Baton Rouge", recipe_json='{"format": "csv", "time": "time", "value": "stage_ft", "unit": "ft"}', note="n"))
    assert crawl.build(conn, LLM(conn, client), get, NOW)["built"] == ["River stage at Baton Rouge"]
    assert "time,stage_ft" in client.calls[1]["messages"][0]["content"]                         # it was shown what the new address returned
    assert conn.execute("SELECT url FROM sources").fetchone()[0] == "https://water.example/api/stage.csv"
    # an address that is not public is refused, and the source is set aside
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://b.example/', 'Other', 1, 'proposal', '[data] x')")
    client.replies += [Built(decision="elsewhere", try_url="http://169.254.169.254/latest/meta-data")]
    assert crawl.build(conn, LLM(conn, client), get, NOW) == {"built": [], "needs_work": 1}
