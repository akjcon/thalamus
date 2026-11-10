import time
from types import SimpleNamespace
import pytest
from thal import db, ingest
from thal.wiki import MAX_PAGE_BYTES, StaleWrite, Wiki, slugify


@pytest.fixture
def wiki(tmp_path):
    return Wiki(tmp_path / "wiki")


def test_roundtrip_and_metadata(wiki):
    p = wiki.write("Strait of Hormuz", "Strait of Hormuz", "chokepoint", "20% of oil.\nSee [[Oil Tankers]] and [[iran|Iran]].", today="2026-11-09")
    assert p.slug == "strait-of-hormuz" and p.kind == "chokepoint" and p.updated == "2026-11-09"
    assert p.links == ["oil-tankers", "iran"]
    again = wiki.read("strait-of-hormuz")
    assert again == p and again.body.startswith("20% of oil.")
    assert wiki.read("missing") is None


def test_writer_must_have_read_the_current_version(wiki):
    a = wiki.write("sugar", "Sugar", "commodity", "v1")
    with pytest.raises(StaleWrite): wiki.write("sugar", "Sugar", "commodity", "blind overwrite")          # no rev
    b = wiki.write("sugar", "Sugar", "commodity", "v2", expect_rev=a.rev)
    with pytest.raises(StaleWrite): wiki.write("sugar", "Sugar", "commodity", "v3", expect_rev=a.rev)     # stale rev
    assert wiki.read("sugar").body == "v2" and b.rev != a.rev
    with pytest.raises(StaleWrite): wiki.write("ghost", "Ghost", "theme", "x", expect_rev="abc")           # deleted/never existed


def test_bad_pages_are_refused(wiki):
    with pytest.raises(ValueError): wiki.write("x", "X", "theme", "   \n")
    with pytest.raises(ValueError): wiki.write("x", "X", "nonsense", "body")
    with pytest.raises(ValueError): wiki.write("x", "X", "theme", "a" * (MAX_PAGE_BYTES + 1))
    with pytest.raises(ValueError): slugify("!!!")
    assert not list(wiki.root.glob("*"))                                   # nothing half-written


def test_path_escape_is_impossible(wiki, tmp_path):
    wiki.write("../../evil", "Evil", "theme", "x")
    assert [p.name for p in wiki.root.iterdir()] == ["evil.md"]
    assert not (tmp_path / "evil.md").exists()


def test_graph_health(wiki):
    wiki.write("a", "A", "theme", "to [[b]] and [[nowhere]]")
    wiki.write("b", "B", "theme", "back to [[a]]")
    wiki.write("c", "C", "theme", "alone, links [[a]]")
    assert wiki.backlinks("a") == ["b", "c"]
    assert wiki.broken_links() == {"a": ["nowhere"]}
    assert wiki.orphans() == ["c"]
    assert wiki.stats()["pages"] == 3 and wiki.stats()["links"] == 4
    assert "- [[a]] (theme," in wiki.index()


def test_search_prefers_title_hits(wiki):
    wiki.write("sugar", "Sugar market", "commodity", "India monsoon, Brazil ethanol.")
    wiki.write("india", "India", "country", "Sugar importer this year. Sugar mills. Monsoon.")
    wiki.write("iran", "Iran", "country", "Hormuz.")
    assert [s for s, _ in wiki.search("sugar shortfall")] == ["sugar", "india"]
    assert wiki.search("zzzz") == []


# ---- ingest ----

def feed(*entries):
    return SimpleNamespace(entries=[dict(e) for e in entries])


def test_parse_cleans_html_and_drops_untitled():
    rows = ingest.parse_feed("Src", feed(
        {"title": " Tanker <b>hit</b> &amp; burning ", "link": "https://x.com/a?utm_source=rss", "summary": "<p>Two   crew missing</p>",
         "published_parsed": time.gmtime(1_780_000_000)},
        {"title": "", "link": "https://x.com/b"}))
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Tanker hit & burning" and r["body"] == "Two crew missing" and r["published_at"] == 1_780_000_000.0


def test_same_story_is_stored_once_across_fetches_and_tracking_params(conn):
    a = ingest.parse_feed("S1", feed({"title": "T", "link": "https://x.com/a?utm_source=rss"}))
    b = ingest.parse_feed("S2", feed({"title": "T again", "link": "https://x.com/a/?utm_medium=x"}))
    assert ingest.store(conn, a, now=1.0) == 1
    assert ingest.store(conn, a, now=2.0) == 0 and ingest.store(conn, b, now=3.0) == 0
    assert conn.execute("SELECT COUNT(*), MIN(fetched_at) FROM items").fetchone()[:] == (1, 1.0)


def test_one_broken_feed_does_not_stop_the_others(conn):
    def fetch(url):
        if "bad" in url: raise OSError("dns")
        return feed({"title": f"story from {url}", "link": url + "/1"})
    rep = ingest.ingest(conn, [{"name": "A", "url": "http://a"}, {"name": "B", "url": "http://bad"}, {"name": "C", "url": "http://c"}], fetch=fetch)
    assert rep["A"] == 1 and rep["C"] == 1 and rep["B"].startswith("error")


def test_items_are_full_text_searchable(conn):
    ingest.store(conn, ingest.parse_feed("S", feed({"title": "Lumpy skin disease reaches France", "link": "u1", "summary": "cattle"},
                                                   {"title": "Fed holds rates", "link": "u2"})))
    hit = conn.execute("SELECT i.title FROM items_fts f JOIN items i ON i.rowid = f.rowid WHERE items_fts MATCH ?",
                       (db.fts_query("cattle disease (France)!"),)).fetchall()
    assert [h[0] for h in hit] == ["Lumpy skin disease reaches France"]
    assert db.fts_query("a | -- ") == ""


def test_shipped_feed_config_loads():
    feeds = ingest.load_feeds()
    assert len(feeds) >= 20 and all(f["url"].startswith("http") for f in feeds)
