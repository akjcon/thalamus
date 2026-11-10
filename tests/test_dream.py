import json
import pytest
from thal import dream, ledger, memory
from thal.broker import PaperBroker
from thal.dream import LessonsOut, PageOut
from thal.llm import LLM
from thal.prices import FakeProvider
from thal.risk import Limits
from thal.wiki import MAX_PAGE_BYTES, Wiki
from .conftest import DAY, T0, series
from .fakes import FakeClient

NOW = T0 + 50 * DAY + 3600
L = Limits(kill_switch="/nonexistent/KILL")


@pytest.fixture
def world(conn, tmp_path):
    prov = FakeProvider({"SPY": series(100, 0.0, 51), "CANE": series(20, 0.0, 51), "XLE": series(50, 0.0, 51),
                         "SGG": series(40, 0.0, 10)})                     # SGG stopped trading 40 days ago
    return conn, Wiki(tmp_path / "wiki"), prov, PaperBroker(conn, prov, L, starting_cash=100_000)


# ---- dream ----

def note(conn, slug="sugar", text="India swings between exporter and importer with the monsoon."):
    return memory.queue_note(conn, slug, slug.title(), "commodity", text, now=NOW)


def test_fold_creates_then_updates_a_page_and_marks_notes_done(world):
    conn, wiki = world[0], world[1]
    note(conn)
    assert dream.fold_page(conn, LLM(conn, FakeClient(PageOut(title="Sugar", kind="commodity", body="v1 body"))), wiki, "sugar", NOW)
    assert wiki.read("sugar").body == "v1 body" and conn.execute("SELECT folded FROM page_notes").fetchone()[0] == 1
    assert not dream.fold_page(conn, LLM(conn, FakeClient()), wiki, "sugar", NOW)             # nothing queued -> no call
    note(conn, text="Brazil is the swing supplier.")
    client = FakeClient(PageOut(title="Sugar", kind="commodity", body="v2 body"))
    dream.fold_page(conn, LLM(conn, client), wiki, "sugar", NOW)
    sent = client.calls[0]["messages"][0]["content"]
    assert "v1 body" in sent and "Brazil is the swing supplier." in sent and "India swings" not in sent
    assert wiki.read("sugar").body == "v2 body"


def test_fold_failure_keeps_notes_queued_and_other_pages_still_fold(world):
    conn, wiki = world[0], world[1]
    note(conn, "a"); note(conn, "b"); note(conn, "c")
    client = FakeClient(PageOut(title="A", kind="theme", body="   "), RuntimeError("api"), PageOut(title="C", kind="theme", body="ok"))
    rep = dream.fold_all(conn, LLM(conn, client), wiki, NOW)
    assert rep["folded"] == ["c"] and set(rep["failed"]) == {"a", "b"}
    assert conn.execute("SELECT COUNT(*) FROM page_notes WHERE folded = 0").fetchone()[0] == 2 and wiki.slugs() == ["c"]


def test_overlong_page_is_cut_not_lost(world):
    conn, wiki = world[0], world[1]
    note(conn)
    dream.fold_page(conn, LLM(conn, FakeClient(PageOut(title="S", kind="commodity", body="line\n" * 5000))), wiki, "sugar", NOW)
    p = wiki.read("sugar")
    assert len(p.body.encode()) <= MAX_PAGE_BYTES and p.body.endswith("(cut: page too long)")


def test_reflect_needs_a_record_and_writes_lessons(world):
    conn, wiki, prov, _ = world
    assert not dream.reflect(conn, LLM(conn, FakeClient()), wiki, NOW)
    ledger.add_bet(conn, symbol="CANE", side="LONG", source="v1", kind="alert", created_at=T0 - 1, tags=["commodity"])
    ledger.mark_all(conn, prov, NOW)
    client = FakeClient(LessonsOut(body="- Shorts lost money (n=12)."), LessonsOut(body="- Updated."))
    assert dream.reflect(conn, LLM(conn, client), wiki, NOW)
    sent = client.calls[0]["messages"][0]["content"]
    assert "By kind" in sent and "By tag" in sent and "(none yet)" in sent
    assert "Shorts lost money" in memory.dashboard(conn, wiki, NOW)                           # lessons reach the top layer
    dream.reflect(conn, LLM(conn, client), wiki, NOW)
    assert wiki.read("lessons").body == "- Updated." and "Shorts lost money" in client.calls[1]["messages"][0]["content"]


def test_full_dream_does_housekeeping_and_reports_health(world):
    conn, wiki, prov, broker = world
    memory.open_fuse(conn, "old", "t", ["w"], expires_days=7, now=NOW - 30 * DAY)
    broker.submit("CANE", "BUY", 10, now=NOW - 5 * DAY)
    ledger.add_bet(conn, symbol="CANE", side="LONG", created_at=T0 - 1)
    note(conn, text="see [[brazil]]")
    conn.execute("INSERT INTO items(id, source, title, fetched_at) VALUES ('i','S','t',1)")
    conn.execute("INSERT INTO events(item_id, at, event_type, summary) VALUES ('i', 1, 'other', 'short')")
    client = FakeClient(PageOut(title="Sugar", kind="commodity", body="Swing supplier is [[brazil]]. " + "x" * 200), LessonsOut(body="- n too small."))
    rep = dream.dream(conn, LLM(conn, client), wiki, prov, broker, now=NOW)
    assert rep["housekeeping"] == {"fuses_expired": 1, "orders_filled": 1, "marks": 3}
    assert rep["fold"]["folded"] == ["sugar"] and rep["reflected"]
    h = rep["health"]
    assert h["broken_links"] == {"sugar": ["brazil"]} and h["notes_waiting"] == 0 and h["bloated"] is True
    assert [c["model"] for c in client.calls] == ["claude-opus-5-5", "claude-fable-5-2"]
