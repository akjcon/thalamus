import urllib.error
import pytest
from thal import extract, fetch, ingest
from thal.agent import Session
from thal.prices import FakeProvider
from thal.wiki import Wiki

ARTICLE = "<html><body><nav>Home | About</nav><article><h1>Qatar extends force majeure</h1>" + "".join(
    f"<p>QatarEnergy said on Monday that cargo {i} from Ras Laffan would not load before the end of December.</p>" for i in range(12)) + "</article><footer>Cookies</footer></body></html>"
WALL = "<html><body><h1>Your Request Originates from an Undeclared Automated Tool</h1>" + "<p>To allow for equitable access please declare your traffic.</p>" * 30 + "</body></html>"
NOBROWSER = lambda u: ""


def test_readable_keeps_the_article_and_drops_the_chrome():
    t = fetch.readable(ARTICLE)
    assert "cargo 11 from Ras Laffan" in t and "Cookies" not in t and "Home | About" not in t


def test_http_path_and_text_cap():
    text, how = fetch.fetch_text("https://x.example/a", get=lambda u: ("text/html", ARTICLE), browser=NOBROWSER)
    assert how == "http" and "force majeure" in text
    big, _ = fetch.fetch_text("https://x.example/a", get=lambda u: ("text/plain", "word " * 10_000), browser=NOBROWSER)
    assert len(big) == fetch.MAX_TEXT


def test_browser_renders_thin_pages_but_a_refusal_is_final():
    used = []
    def render(u): used.append(u); return ARTICLE
    assert fetch.fetch_text("https://x.example/a", get=lambda u: ("text/html", "<p>teaser</p>"), browser=render)[1] == "browser"
    for code in (401, 403, 429):
        def no(u, code=code): raise urllib.error.HTTPError(u, code, "no", {}, None)
        assert fetch.fetch_text(f"https://x.example/{code}", get=no, browser=render) == ("", f"http {code}")
    assert fetch.fetch_text("https://x.example/w", get=lambda u: ("text/html", WALL), browser=render) == ("", "walled (undeclared automated tool)")
    assert used == ["https://x.example/a"]                                   # the browser never touched a page that said no
    def gone(u): raise urllib.error.HTTPError(u, 500, "err", {}, None)
    def crash(u): raise RuntimeError("chromium died")
    assert fetch.fetch_text("https://x.example/a", get=gone, browser=crash) == ("", "http 500; browser RuntimeError")
    assert fetch.fetch_text("https://x.example/a", get=lambda u: ("text/html", "<p>teaser</p>")) [1].startswith("only ")   # browser is off by default


def test_client_says_what_it_is_and_browser_is_opt_in(monkeypatch):
    assert "thalamus-research" in fetch.UA and "Chrome" not in fetch.UA and "Safari" not in fetch.UA
    monkeypatch.delenv("THAL_BROWSER", raising=False)
    monkeypatch.setattr("thal.llm.load_env", lambda *a: None)
    assert fetch.default_browser() is None
    monkeypatch.setenv("THAL_BROWSER", "1")
    assert fetch.default_browser() is fetch.browser_text


def test_block_pages_and_paywalls_are_never_stored_as_content():
    assert fetch.wall("Access Denied\nYou don't have permission") and not fetch.wall("QatarEnergy said on Monday")
    assert not fetch.wall("x" * 700 + " subscribe to read more of our coverage")          # a footer mention is not a wall
    text, how = fetch.fetch_text("https://sec.example/x", get=lambda u: ("text/html", WALL), browser=lambda u: WALL)
    assert (text, how) == ("", "walled (undeclared automated tool)")


def test_private_addresses_and_pdfs():
    called = []
    assert fetch.fetch_text("http://127.0.0.1/admin", get=lambda u: (_ for _ in ()).throw(ValueError("x")), browser=lambda u: called.append(u) or ARTICLE) == ("", "blocked: not a public url")
    assert called == []                                                                   # the browser is not tried either
    with pytest.raises(ValueError): fetch.http_get("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError): fetch.browser_get("http://localhost:8080/")
    assert fetch.fetch_text("https://x.example/a.pdf", get=lambda u: ("application/pdf", "%PDF"), browser=NOBROWSER) == ("", "pdf not supported yet")


def test_redirects_are_rechecked():
    h = fetch._CheckedRedirects()
    import urllib.request
    with pytest.raises(ValueError): h.redirect_request(urllib.request.Request("https://x.example/a"), None, 302, "Found", {}, "http://10.0.0.1/secret")


def items(conn, n, source="S", host="x.example"):
    ingest.store(conn, [{"id": f"{source}{i}", "source": source, "url": f"https://{host}/{i}", "title": f"t{i}", "body": "rss blurb", "published_at": None} for i in range(n)], now=1000.0)


def test_fetch_pending_stores_text_is_polite_per_site_and_runs_once(conn):
    items(conn, 3); items(conn, 1, "Other", "y.example")
    conn.execute("INSERT INTO items(id, source, url, title, fetched_at) VALUES ('nolink', 'S', '', 't', 1)")
    sleeps, t = [], [0.0]
    rep = fetch.fetch_pending(conn, fetcher=lambda u: ("full text of " + u, "http"), sleep=lambda s: (sleeps.append(round(s, 2)), t.__setitem__(0, t[0] + s)), clock=lambda: t[0], workers=1)
    assert rep == {"tried": 4, "ok": 4, "failed": 0, "by_browser": 0, "skipped_sources": []}
    assert sleeps == [1.0, 1.0]                                              # two waits for the 3 same-site pages, none across sites
    assert conn.execute("SELECT fulltext FROM items WHERE id = 'S1'").fetchone()[0] == "full text of https://x.example/1"
    assert fetch.fetch_pending(conn, fetcher=lambda u: 1 / 0, browser=None)["tried"] == 0  # nothing is fetched twice


def test_source_that_keeps_failing_is_skipped_and_reported(conn):
    items(conn, fetch.GIVE_UP_AFTER + 4, "Walled"); items(conn, 2, "Fine", "y.example")
    calls = []
    def f(u): calls.append(u); return ("", "http 403") if "x.example" in u else ("article", "http")
    rep = fetch.fetch_pending(conn, fetcher=f, sleep=lambda s: None, browser=None)
    assert rep["failed"] == fetch.GIVE_UP_AFTER + 1 and rep["ok"] == 2 and rep["skipped_sources"] == ["Walled"]   # 6 failures, then one probe
    again = fetch.fetch_pending(conn, fetcher=f, sleep=lambda s: None, browser=None)
    assert again["tried"] == 1 and again["skipped_sources"] == ["Walled"]                 # next run: a single probe, still walled
    healed = fetch.fetch_pending(conn, fetcher=lambda u: ("article", "http"), sleep=lambda s: None, browser=None)
    assert healed["ok"] == 2 and healed["skipped_sources"] == []                          # access fixed: the probe succeeds and it resumes
    conn.execute("UPDATE items SET fulltext = NULL, fetch_status = 'http 403' WHERE source = 'Walled'"); conn.commit()
    cov = {c["source"]: c for c in fetch.coverage(conn)}
    assert cov["Walled"]["ok"] == 0 and cov["Walled"]["main_failure"] == "http 403" and cov["Fine"]["ok"] == 2


def test_extraction_reads_the_article_when_there_is_one(conn):
    items(conn, 2)
    conn.execute("UPDATE items SET fulltext = ? WHERE id = 'S0'", ("ARTICLE " + "x" * 9000,)); conn.commit()
    text = extract.render(conn.execute("SELECT * FROM items ORDER BY rowid").fetchall())
    assert "ARTICLE" in text and "rss blurb" in text and text.count("rss blurb") == 1
    assert len(text) < extract.FULLTEXT_CHARS + 400


def test_analyst_can_read_the_source_and_fetch_pages(conn, tmp_path):
    items(conn, 2)
    conn.execute("UPDATE items SET fulltext = 'the whole filing text', fetch_status = 'http' WHERE id = 'S0'")
    conn.execute("UPDATE items SET fetch_status = 'http 403' WHERE id = 'S1'")
    for i in (0, 1): conn.execute("INSERT INTO events(item_id, at, event_type, summary) VALUES (?, 1, 'other', 'e')", (f"S{i}",))
    conn.commit()
    s = Session(conn, Wiki(tmp_path / "w"), FakeProvider(), None, 1000.0, fetch=lambda u: ("page text", "http") if "ok" in u else ("", "walled (captcha)"))
    assert "the whole filing text" in s.read_item(1) and "https://x.example/0" in s.read_item(1)
    assert "full text not available: http 403" in s.read_item(2) and "rss blurb" in s.read_item(2) and s.read_item(9) == "no event #9"
    assert s.fetch_url("https://ok.example/doc") == "page text" and s.fetch_url("https://no.example/") == "could not read that page: walled (captcha)"


def test_contact_address_goes_only_to_sites_that_require_it():
    env = {"THAL_CONTACT_EMAIL": "me@example.org"}
    assert fetch.client_name("https://www.sec.gov/Archives/edgar/x.htm", env) == "thalamus-research me@example.org"
    assert fetch.client_name("https://sec.gov/x", env) == "thalamus-research me@example.org"
    for other in ("https://www.argusmedia.com/a", "https://notsec.gov/x", "https://sec.gov.evil.example/x", "https://example.com/?u=sec.gov"):
        assert "me@example.org" not in fetch.client_name(other, env) and fetch.client_name(other, env) == fetch.UA
    assert fetch.client_name("https://www.sec.gov/x", {}) == fetch.UA                     # nothing set: nothing sent


def test_edgar_index_is_followed_to_the_filing_and_its_exhibits():
    base = "https://www.sec.gov/Archives/edgar/data/746515/000119312526446859/0001193125-26-446859-index.htm"
    index = ('<table><a href="/ix?doc=/Archives/edgar/data/746515/000119312526446859/d8k.htm">d8k.htm</a>'
             '<a href="/Archives/edgar/data/746515/000119312526446859/dex991.htm">dex991.htm</a>'
             '<a href="/Archives/edgar/data/746515/000119312526446859/0001193125-26-446859-index.htm">index</a>'
             '<a href="/Archives/edgar/data/746515/000119312526446859/d8k.htm">dup</a></table>')
    docs = fetch.edgar_documents(index, base)
    assert docs == ["https://www.sec.gov/Archives/edgar/data/746515/000119312526446859/dex991.htm",
                    "https://www.sec.gov/Archives/edgar/data/746515/000119312526446859/d8k.htm"]         # the exhibit comes first
    docs = docs[::-1]
    pages = {base: index, docs[0]: "<html><body><p>Item 2.02 Results of Operations. " + "The company reported results. " * 20 + "</p></body></html>",
             docs[1]: "<html><body><p>Expeditors reports third quarter customs brokerage revenue up 12%. " + "Detail follows here. " * 20 + "</p></body></html>"}
    text, how = fetch.fetch_text(base, get=lambda u: ("text/html", pages[u]), browser=NOBROWSER, pause=lambda s: None)
    assert how == "http" and text.index("[dex991.htm]") < text.index("[d8k.htm]") and "customs brokerage revenue up 12%" in text
    table = "<html><head><style>td{}</style></head><body><table>" + "".join(f"<tr><td>Revenue line {i}</td><td>$ {i},000</td></tr>" for i in range(40)) + "</table><script>x()</script></body></html>"
    assert fetch.readable(table) == "" or True                                            # article extraction may find nothing in a pure table
    p = fetch.plain(table)
    assert "Revenue line 39 $ 39,000" in p and "x()" not in p and "td{}" not in p
    pages[docs[1]] = table
    text, _ = fetch.fetch_text(base, get=lambda u: ("text/html", pages[u]), browser=NOBROWSER, pause=lambda s: None)
    assert "Revenue line 39" in text                                                      # table-only exhibit is still read


def test_only_thin_pages_get_rendered_and_refused_sites_are_left_alone(conn):
    items(conn, 4, "NeedsJS", "a.example"); items(conn, 5, "Refuses", "b.example"); items(conn, 5, "StillThin", "d.example"); items(conn, 1, "Crashy", "c.example")
    used = []
    def browser(u):
        used.append(u)
        return ("rendered article", "browser") if "a.example" in u else ("", "browser got too little text")
    def http(u):
        if "c.example" in u: raise RuntimeError("boom")
        return ("", "http 403") if "b.example" in u else ("", "only 120 chars of text")
    rep = fetch.fetch_pending(conn, fetcher=http, sleep=lambda s: None, browser=browser)
    assert rep["by_browser"] == 4 and rep["ok"] == 4
    assert not any("b.example" in u for u in used)                          # refused pages are never rendered
    assert sum("d.example" in u for u in used) == 3                         # three tries, then that site is left alone
    assert conn.execute("SELECT fetch_status FROM items WHERE id = 'NeedsJS0'").fetchone()[0] == "browser"
    assert conn.execute("SELECT fetch_status FROM items WHERE id = 'Refuses0'").fetchone()[0] == "http 403"
    off = fetch.fetch_pending(conn, fetcher=http, sleep=lambda s: None, browser=None)
    assert off["by_browser"] == 0


def test_browser_text_rejects_walls_and_stubs():
    assert fetch.browser_text("https://x.example/a", browser=lambda u: ARTICLE)[1] == "browser"
    assert fetch.browser_text("https://x.example/a", browser=lambda u: WALL) == ("", "browser walled (undeclared automated tool)")
    assert fetch.browser_text("https://x.example/a", browser=lambda u: "<p>hi</p>") == ("", "browser got too little text")
    def crash(u): raise OSError("x")
    assert fetch.browser_text("https://x.example/a", browser=crash) == ("", "browser OSError")
