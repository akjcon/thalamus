import json
import pytest
from thal import ledger, memory
from thal.prices import FakeProvider
from thal.wiki import Wiki
from .conftest import DAY, T0, series

NOW = 1_790_000_000.0


@pytest.fixture
def wiki(tmp_path):
    return Wiki(tmp_path / "wiki")


def add_event(conn, summary, mag=3, nov=3, connected=0, commodities=()):
    conn.execute("INSERT OR IGNORE INTO items(id, source, title, fetched_at) VALUES ('i','S','t',1)")
    cur = conn.execute("INSERT INTO events(item_id, at, event_type, summary, commodities, magnitude, novelty, connected)"
                       " VALUES ('i', ?, 'other', ?, ?, ?, ?, ?)", (NOW - DAY, summary, json.dumps(list(commodities)), mag, nov, connected))
    conn.commit()
    return cur.lastrowid


def fuse(conn, **kw):
    return memory.open_fuse(conn, kw.get("title", "India sugar shortfall"), "India must import; Brazil is the only offset.",
                            kw.get("watch", ["monsoon data", "mill output"]), kw.get("days", 90), now=NOW)


def test_fuse_must_say_what_it_waits_for(conn):
    with pytest.raises(ValueError): memory.open_fuse(conn, "t", "thesis", ["  "], now=NOW)
    with pytest.raises(ValueError): memory.open_fuse(conn, " ", "thesis", ["x"], now=NOW)
    f = conn.execute("SELECT * FROM fuses WHERE id = ?", (fuse(conn, days=99999),)).fetchone()
    assert f["status"] == "open" and f["expires_at"] == NOW + 365 * DAY            # expiry is capped


def test_open_fuse_cap(conn):
    for i in range(memory.MAX_OPEN_FUSES): fuse(conn, title=f"f{i}")
    with pytest.raises(ValueError, match="already open"): fuse(conn)


def test_evidence_only_on_open_fuses_real_events_and_once(conn):
    f, e = fuse(conn), add_event(conn, "monsoon 20% below normal")
    assert memory.add_evidence(conn, f, e, "supports", "rain deficit", NOW)
    assert not memory.add_evidence(conn, f, e, "supports", "again", NOW)           # duplicate
    assert not memory.add_evidence(conn, f, 9999, "supports", "ghost event", NOW)
    assert not memory.add_evidence(conn, 9999, e, "supports", "ghost fuse", NOW)
    with pytest.raises(ValueError): memory.add_evidence(conn, f, e, "maybe", "x", NOW)
    assert memory.close_fuse(conn, f, "dead", NOW) and not memory.close_fuse(conn, f, "dead", NOW)
    assert not memory.add_evidence(conn, f, add_event(conn, "later"), "supports", "too late", NOW)
    assert "[supports] rain deficit" in memory.fuse_text(conn, f)


def test_fuses_expire(conn):
    a, b = fuse(conn, title="short", days=7), fuse(conn, title="long", days=90)
    assert memory.expire_fuses(conn, NOW + 30 * DAY) == 1
    assert [r["status"] for r in conn.execute("SELECT status FROM fuses ORDER BY id")] == ["expired", "open"]


def test_dashboard_shows_rules_record_fuses_and_index_and_stays_small(conn, wiki):
    assert "none yet" in memory.dashboard(conn, wiki, NOW) and "no scored bets yet" in memory.dashboard(conn, wiki, NOW)
    ledger.add_bet(conn, symbol="AAA", side="LONG", source="v1", kind="alert", created_at=T0 - 1)
    ledger.mark_all(conn, FakeProvider({"AAA": series(100, 0.01, 40), "SPY": series(100, 0.0, 40)}), T0 + 100 * DAY)
    fuse(conn); wiki.write("sugar", "Sugar", "commodity", "x"); wiki.write("lessons", "Lessons", "rule", "Shorts lost money.")
    d = memory.dashboard(conn, wiki, NOW)
    assert "House rules" in d and "v1 at 4w: n=1" in d and "#1 India sugar shortfall" in d and "[[sugar]]" in d and "Shorts lost money." in d
    for i in range(300): wiki.write(f"page-{i}", f"Page {i} " + "x" * 80, "theme", "body")
    big = memory.dashboard(conn, wiki, NOW)
    assert len(big.splitlines()) <= memory.MAX_DASHBOARD_LINES and len(big.encode()) <= memory.MAX_DASHBOARD_BYTES
    assert "search the wiki" in big and "#1 India sugar shortfall" in big          # fuses survive the cut, the long index does not
