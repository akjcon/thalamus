import calendar
import json
import pytest
from thal import memory, serve
from thal.paths import data_dir, db_path, kill_switch, wiki_dir
from thal.wiki import Wiki

def at(day, hour, minute=0):                      # November 2026: the 8th is a Sunday, the 9th a Monday
    return float(calendar.timegm((2026, 11, day, hour, minute, 0)))


def test_schedule_through_a_monday_and_a_sunday(conn):
    assert serve.due(conn, at(9, 1)) == ["intake"]                         # fresh start, before any daily slot
    serve.mark_run(conn, "intake", at(9, 1))
    assert serve.due(conn, at(9, 2)) == []                                 # intake is every four hours
    assert serve.due(conn, at(9, 3, 5)) == ["dream"]
    serve.mark_run(conn, "dream", at(9, 3, 5))
    assert serve.due(conn, at(9, 5, 30)) == ["intake"]
    serve.mark_run(conn, "intake", at(9, 5, 30))
    assert serve.due(conn, at(9, 12, 1)) == ["analyst"]                    # the analyst job does its own intake
    serve.mark_run(conn, "analyst", at(9, 12, 1)); serve.mark_run(conn, "intake", at(9, 12, 1))
    assert serve.due(conn, at(9, 13)) == [] and serve.due(conn, at(9, 16, 2)) == ["intake"]
    assert "scout" not in serve.due(conn, at(9, 6))                        # Monday: no scout
    assert serve.due(conn, at(15, 6)) == ["dream", "scout", "intake"]      # the next Sunday morning
    assert serve.due(conn, at(15, 4)) == ["dream", "intake"]               # before the scout's hour


def test_restart_does_not_repeat_a_job_that_already_ran_today(conn):
    serve.mark_run(conn, "analyst", at(9, 12, 5)); serve.mark_run(conn, "dream", at(9, 3)); serve.mark_run(conn, "intake", at(9, 12, 5))
    assert serve.due(conn, at(9, 12, 6)) == []
    assert serve.due(conn, at(10, 12, 0)) == ["dream", "analyst"]          # a day missed entirely: both run once


def test_a_failing_job_is_reported_once_and_does_not_loop(conn, monkeypatch):
    calls, said = [], []
    def boom(job, *a): calls.append(job); raise RuntimeError("yahoo down")
    monkeypatch.setattr(serve, "run_job", boom)
    assert serve.tick(conn, None, None, None, None, said.append, at(9, 12, 1)) == ["dream", "analyst"]
    assert serve.tick(conn, None, None, None, None, said.append, at(9, 12, 2)) == []
    assert calls == ["dream", "analyst"] and all("failed: RuntimeError: yahoo down" in s for s in said) and len(said) == 2


def test_quiet_intake_is_not_posted_but_other_jobs_are(conn, monkeypatch):
    said = []
    monkeypatch.setattr(serve, "run_job", lambda job, *a: f"{job} ok")
    serve.tick(conn, None, None, None, None, said.append, at(9, 1))
    assert said == []
    serve.tick(conn, None, None, None, None, said.append, at(9, 3, 1))
    assert said == ["dream ok"]


def test_discord_finds_the_channel_by_name_splits_long_messages_and_never_raises():
    sent = []
    def send(method, path, body=None):
        sent.append((method, path, body))
        if path == "/users/@me/guilds": return [{"id": "g1"}]
        if path.endswith("/channels"): return [{"id": "c1", "name": "alerts"}, {"id": "c2", "name": "thal-logs"}]
        return {}
    d = serve.Discord("tok", send=send)
    assert d.post("x" * 4000)
    posts = [s for s in sent if s[0] == "POST"]
    assert [p[1] for p in posts] == ["/channels/c2/messages"] * 3 and all(len(p[2]["content"]) <= 1900 for p in posts)
    assert d.post("again") and sum(s[1] == "/users/@me/guilds" for s in sent) == 1          # channel lookup is cached
    assert not serve.Discord(None, send=send).post("no token")
    def down(*a): raise OSError("net")
    assert not serve.Discord("tok", send=down).post("x")
    assert not serve.Discord("tok", channel="missing", send=send).post("x")


def test_seed_loads_only_into_an_empty_database(conn, tmp_path, monkeypatch):
    seed = tmp_path / "seed"; seed.mkdir()
    (seed / "memory.json").write_text(json.dumps({"fuses": [{"title": "Suez return", "thesis": "t", "watch": ["loops"]}],
        "notes": [{"page": "lng", "title": "LNG", "kind": "commodity", "note": "Qatar ships only via Hormuz."}],
        "pages": {"lessons": {"title": "Lessons", "kind": "rule", "body": "- Shorts lost."}},
        "sources": [{"name": "Kpler Blog", "url": "https://k.example/rss", "status": "trial", "added_by": "scout", "why": "flows"},
                    {"name": "Dead", "url": "https://d.example/rss", "status": "retired", "added_by": "seed"}]}))
    monkeypatch.setattr(serve, "ROOT", tmp_path)
    wiki = Wiki(tmp_path / "wiki")
    assert serve.load_seed(conn, wiki, 1000.0)
    assert conn.execute("SELECT title FROM fuses").fetchone()[0] == "Suez return" and wiki.read("lessons").body == "- Shorts lost."
    assert conn.execute("SELECT slug FROM page_notes").fetchone()[0] == "lng"
    from thal import sources
    assert sources.live(conn) == [{"name": "Kpler Blog", "url": "https://k.example/rss"}] and sources.seed(conn, 1.0) == 0   # the starter list does not override it
    assert not serve.load_seed(conn, wiki, 2000.0) and conn.execute("SELECT COUNT(*) FROM fuses").fetchone()[0] == 1


def test_state_moves_to_the_volume_when_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("THAL_DATA_DIR", raising=False)
    assert data_dir().name == "data" and wiki_dir().name == "wiki" and wiki_dir().parent == data_dir().parent
    monkeypatch.setenv("THAL_DATA_DIR", str(tmp_path / "vol"))
    assert db_path() == tmp_path / "vol" / "thal.db" and wiki_dir() == tmp_path / "vol" / "wiki" and kill_switch() == tmp_path / "vol" / "KILL"
    from thal import db, risk
    c = db.connect(); c.close()
    assert (tmp_path / "vol" / "thal.db").exists() and Wiki().root == tmp_path / "vol" / "wiki"
    (tmp_path / "vol" / "KILL").write_text("stop")
    assert risk.check(risk.OrderRequest("AAA", "BUY", 1, 10), risk.Portfolio(cash=1000), risk.Limits(), 0, {}).reasons == ("kill switch is on",)
