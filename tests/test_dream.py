import json
import pytest
from thal import dream, ledger, memory
from thal.agent import digest, run_session
from thal.broker import PaperBroker
from thal.dream import DreamSession, LessonsOut
from thal.llm import LLM
from thal.prices import FakeProvider
from thal.risk import Limits
from thal.wiki import Wiki
from .conftest import DAY, T0, series
from .fakes import FakeClient
from .test_agent import Script, resp, text, use

NOW = T0 + 50 * DAY + 3600


@pytest.fixture
def world(conn, tmp_path):
    prov = FakeProvider({"SPY": series(100, 0.0, 51), "CANE": series(20, 0.0, 51), "XLE": series(50, 0.0, 51)})
    return conn, Wiki(tmp_path / "wiki"), prov, PaperBroker(conn, prov, Limits(kill_switch="/nonexistent/KILL"), starting_cash=100_000)


def note(conn, slug="sugar", text="India swings between exporter and importer with the monsoon."):
    return memory.queue_note(conn, slug, slug.title(), "commodity", text, now=NOW)


def event(conn, summary, source="S1", at=NOW - DAY, entities=(), connected=0):
    iid = f"i{conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]}"
    conn.execute("INSERT INTO items(id, source, url, title, body, fulltext, fetched_at) VALUES (?,?,?,?,?,?,1)", (iid, source, f"https://x.example/{iid}", "t", "blurb", f"full article for {summary}"))
    cur = conn.execute("INSERT INTO events(item_id, at, event_type, summary, entities, connected) VALUES (?,?,'other',?,?,?)", (iid, at, summary, json.dumps(list(entities)), connected))
    conn.commit()
    return cur.lastrowid


def test_notes_are_listed_by_page_and_folded_when_the_page_is_written(world):
    conn, wiki = world[0], world[1]
    s = DreamSession(conn, wiki, NOW)
    assert s.notes_waiting() == "no notes are waiting" and s.list_pages() == "no pages yet"
    note(conn); note(conn, text="Brazil is the swing supplier."); note(conn, "port-of-santos", "Handles most Brazilian sugar.")
    listed = s.notes_waiting()
    assert "## sugar (no page yet; suggested kind commodity)" in listed and "Brazil is the swing supplier." in listed and "## port-of-santos" in listed
    assert s.write_page("Sugar", "Sugar", "commodity", "India follows the monsoon; Brazil swings. See [[port-of-santos]].") == "wrote sugar (65 bytes); 2 queued note(s) folded"
    assert "## sugar" not in s.notes_waiting() and "port-of-santos" in s.notes_waiting()
    assert "Broken links: sugar -> port-of-santos" in s.list_pages() and "Pages nothing links to: sugar" in s.list_pages()
    assert "wrote sugar" in s.write_page("sugar", "Sugar", "commodity", "v2")          # it may rewrite its own page freely
    assert wiki.read("sugar").body == "v2" and s.written == ["sugar"] and "v2" in s.read_page("sugar")


def test_dream_cannot_write_lessons_or_bad_pages_and_errors_are_returned_not_raised(world):
    conn, wiki = world[0], world[1]
    s = DreamSession(conn, wiki, NOW)
    assert s.call("write_page", {"page": "Lessons", "title": "L", "kind": "rule", "body": "x"})[1]
    assert s.call("write_page", {"page": "x", "title": "X", "kind": "legacy", "body": "x"})[1]
    assert s.call("write_page", {"page": "x", "title": "X", "kind": "theme", "body": "   "})[1]
    assert s.call("propose_bet", {"symbol": "CANE"})[1] and s.call("open_fuse", {})[1]      # it has no trading or fuse-opening tools
    assert wiki.slugs() == [] and s.counts["tool_errors"] == 5


def test_duplicate_candidates_find_the_same_story_but_not_merely_the_same_topic(world):
    conn = world[0]
    a = event(conn, "An explosion at the Habas steel plant in Aliaga, Turkey killed at least 17 people.", "BBC", entities=["Habas"])
    b = event(conn, "Explosion at Habas steel plant in Aliaga Turkey killed at least 16 people after a crane collapse.", "AJ", entities=["Habas"])
    c = event(conn, "Turkey raised steel import tariffs on flat products to 25 percent.", "Argus")
    d = event(conn, "An explosion at the Habas steel plant in Aliaga, Turkey killed at least 17 people.", "Old", at=NOW - 6 * DAY)
    pairs = [(x["id"], y["id"]) for _, x, y in dream.duplicate_pairs(conn, NOW)]
    assert pairs == [(a, b)]                                               # c is a different event; d is too far apart in time
    out = DreamSession(conn, world[1], NOW).duplicate_candidates()
    assert f"[{a}]" in out and f"[{b}]" in out and f"[{c}]" not in out


def test_merge_keeps_one_event_counts_sources_moves_evidence_and_hides_duplicates(world):
    conn, wiki, prov, broker = world
    a = event(conn, "Explosion at Habas steel plant", "BBC"); b = event(conn, "Habas steel plant blast", "AJ"); c = event(conn, "Blast at Habas", "BBC")
    f = memory.open_fuse(conn, "Turkish steel", "t", ["w"], now=NOW)
    memory.add_evidence(conn, f, b, "supports", "plant offline", NOW); memory.add_evidence(conn, f, a, "supports", "same", NOW)
    s = DreamSession(conn, wiki, NOW)
    assert s.merge_events(a, [b, c, a, 999, b]) == f"merged 2 event(s) into #{a}"
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM events")}
    assert rows[a]["sources"] == 2 and rows[b]["dup_of"] == a and rows[c]["dup_of"] == a and rows[b]["connected"] == 1
    assert [r["event_id"] for r in conn.execute("SELECT event_id FROM fuse_evidence WHERE event_id IS NOT NULL")] == [a]
    assert s.call("merge_events", {"keep_id": b, "duplicate_ids": [a]})[1]                  # a duplicate cannot become a keeper
    assert f"[{b}]" not in s.search_events("Habas steel") and "(2 sources)" in s.search_events("Habas steel")
    assert 'sources="2"' in digest(conn.execute("SELECT * FROM events WHERE id = ?", (a,)).fetchall())
    client = Script(resp("end_turn", text("nothing to do")))
    rep = run_session(conn, LLM(conn, client), wiki, prov, broker, now=NOW)
    assert rep["events"] == 1 and "Habas steel plant blast" not in client.calls[0]["messages"][0]["content"]   # the analyst sees it once
    assert "full article for Explosion at Habas" in s.read_item(a) and s.read_item(999) == "no event #999"


def test_dream_can_close_a_fuse_with_a_reason(world):
    conn = world[0]
    f = memory.open_fuse(conn, "India sugar shortfall", "t", ["monsoon"], now=NOW)
    s = DreamSession(conn, world[1], NOW)
    assert "#1 India sugar shortfall [0 evidence, 90d left]" in s.list_fuses() and "Waiting for: monsoon" in s.read_fuse(f)
    assert s.call("close_fuse", {"fuse_id": f, "reason": " "})[1]
    assert s.close_fuse(f, "USDA forecasts recovery") == "closed" and "not open" in s.close_fuse(f, "again")
    assert "closed in dream: USDA forecasts recovery" in memory.fuse_text(conn, f) and s.list_fuses() == "no open fuses"


def test_dream_session_loop_and_first_message(world):
    conn, wiki = world[0], world[1]
    note(conn)
    a = event(conn, "Explosion at Habas steel plant in Aliaga killed 17 people", "BBC"); b = event(conn, "Explosion at Habas steel plant in Aliaga killed 16 people", "AJ")
    client = Script(resp("tool_use", use(1, "notes_waiting"), use(2, "duplicate_candidates")),
                    resp("tool_use", use(3, "write_page", page="sugar", title="Sugar", kind="commodity", body="India follows the monsoon."),
                         use(4, "merge_events", keep_id=a, duplicate_ids=[b])),
                    resp("end_turn", text("Folded one page and merged one duplicate.")))
    rep = dream.dream_session(conn, LLM(conn, client), wiki, NOW)
    assert rep["pages_written"] == ["sugar"] and rep["merged"] == 1 and rep["finished"] and rep["summary"].startswith("Folded")
    first = client.calls[0]
    assert first["model"] == "claude-fable-5-2" and "1 notes waiting" in first["messages"][0]["content"] and "1 likely duplicate pairs" in first["messages"][0]["content"]
    assert {t["name"] for t in first["tools"]} == {t["name"] for t in dream.TOOLS} and not any("bet" in t["name"] or "forecast" in t["name"] for t in first["tools"])


def test_reflect_needs_a_record_and_writes_lessons(world):
    conn, wiki, prov, _ = world
    assert not dream.reflect(conn, LLM(conn, FakeClient()), wiki, NOW)
    ledger.add_bet(conn, symbol="CANE", side="LONG", source="v1", kind="alert", created_at=T0 - 1, tags=["commodity"])
    ledger.mark_all(conn, prov, NOW)
    client = FakeClient(LessonsOut(body="- Shorts lost money (n=12)."), LessonsOut(body="- Updated."))
    assert dream.reflect(conn, LLM(conn, client), wiki, NOW)
    sent = client.calls[0]["messages"][0]["content"]
    assert "By kind" in sent and "By tag" in sent and "Forecasts (Brier vs baseline)" in sent and "(none yet)" in sent
    assert "Shorts lost money" in memory.dashboard(conn, wiki, NOW)
    dream.reflect(conn, LLM(conn, client), wiki, NOW)
    assert wiki.read("lessons").body == "- Updated." and "Shorts lost money" in client.calls[1]["messages"][0]["content"]


def test_full_dream_does_housekeeping_session_and_health(world):
    conn, wiki, prov, broker = world
    memory.open_fuse(conn, "old", "t", ["w"], expires_days=7, now=NOW - 30 * DAY)
    broker.submit("CANE", "BUY", 10, now=NOW - 5 * DAY)
    ledger.add_bet(conn, symbol="CANE", side="LONG", created_at=T0 - 1)
    note(conn, text="see [[brazil]]"); event(conn, "short")
    conn.execute("INSERT INTO page_notes(at, slug, title, kind, note) VALUES (1, 'lessons', 'L', 'rule', 'left over from an old version')"); conn.commit()

    class Both(Script):
        pass
    lessons = __import__("types").SimpleNamespace(parsed_output=LessonsOut(body="- n too small."), content=[], stop_reason="end_turn", model="claude-fable-5-2",
                                                  usage=__import__("types").SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    client = Both(resp("tool_use", use(1, "write_page", page="sugar", title="Sugar", kind="commodity", body="Swing supplier is [[brazil]]. " + "x" * 200)),
                  resp("end_turn", text("done")), lessons)
    rep = dream.dream(conn, LLM(conn, client), wiki, prov, broker, now=NOW)
    assert rep["housekeeping"] == {"fuses_expired": 1, "bets_expired": 0, "orders_filled": 1, "marks": 3, "forecasts_resolved": 0}
    assert rep["session"]["pages_written"] == ["sugar"] and rep["reflected"]
    h = rep["health"]
    assert h["broken_links"] == {"sugar": ["brazil"]} and h["notes_waiting"] == 0 and h["bloated"] is True and h["duplicates_merged"] == 0
    assert all(c["model"] == "claude-fable-5-2" for c in client.calls) and len(client.calls) == 3
