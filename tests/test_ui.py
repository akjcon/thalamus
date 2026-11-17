import base64
import urllib.error
import urllib.request
import pytest
from thal import db, ledger, serve, ui

NOW = 1_795_000_000.0
HOUR, DAY = 3600.0, 86400.0


def fill(conn):
    conn.execute("INSERT INTO sources(name, url, status, added_at, kind, last_ok_at, why) VALUES ('Port <b>Notices</b>', 'https://p.example/x', 'trial', 1, 'page', ?, 'w')", (NOW - HOUR,))
    conn.execute("INSERT INTO sources(name, url, status, added_at, fails) VALUES ('Dead Feed', 'https://d.example/', 'retired', 1, 5)")
    conn.execute("INSERT INTO items(id, source, title, fetched_at) VALUES ('i1', 'Port <b>Notices</b>', 't', ?)", (NOW - HOUR,))
    conn.execute("INSERT INTO events(item_id, at, event_type, summary, magnitude) VALUES ('i1', ?, 'x', 'Strait <script>alert(1)</script> closed', 4)", (NOW,))
    for d, v in (("2026-11-15", 60.0), ("2026-11-16", 66.0), ("2026-11-17", 73.0)):
        conn.execute("INSERT INTO readings(source, day, at, value, unit) VALUES ('Rhine level at Kaub', ?, ?, ?, 'cm')", (d, NOW, v))
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://ais.example/', 'Vessel tracking', 1, 'proposal', '[paid] about $500 a month')")
    conn.execute("INSERT INTO candidates(url, name, found_at, status, note) VALUES ('https://js.example/', 'Scripted', 1, 'needs_work', '[data] x | not wired in: needs a browser')")
    conn.execute("INSERT INTO job_log(at, job, ok, summary) VALUES (?, 'dream', 0, 'Dream failed: RuntimeError: disk full')", (NOW - HOUR,))
    conn.execute("INSERT INTO job_log(at, job, ok, summary) VALUES (?, 'overseer', 1, '**Overseer** All jobs are running.\nFine.')", (NOW - HOUR,))
    serve.mark_run(conn, "intake", NOW - 9 * HOUR); serve.mark_run(conn, "analyst", NOW - HOUR); serve.mark_run(conn, "dream", NOW - HOUR)
    conn.execute("INSERT INTO kv(k, v) VALUES ('running_job', ?)", (f"crawl|{NOW}",))
    conn.execute("INSERT INTO runs(at, kind, model, input, output, cost) VALUES (?, 'analyst', 'm', '', '', 5.25)", (NOW - HOUR,))
    ledger.add_bet(conn, symbol="DHT", side="LONG", created_at=NOW - 3 * DAY, thesis="Tankers reroute", horizon_days=30, benchmark="SEA", kind="shadow")
    conn.commit()


def test_page_shows_what_is_running_and_escapes_everything(conn):
    fill(conn)
    html = ui.page(conn, NOW)
    state = lambda job: html.split(f"<b>{job}</b></td><td class=nw>")[1].split("<")[0]
    assert (state("crawl"), state("dream"), state("intake"), state("analyst"), state("scout")) == ("running", "failed", "overdue", "ok", "not yet run")
    assert "$5.25" in html and "Rhine level at Kaub" in html and "<b>73</b> cm" in html and "<polyline" in html
    assert "LONG DHT" in html and "shadow" in html and "Tankers reroute" in html
    assert "<script>alert(1)" not in html and "Strait &lt;script&gt;" in html and "Port &lt;b&gt;Notices&lt;/b&gt;" in html
    assert "Waiting for you · 2" in html and "value=approve" in html and "value=retry" in html
    assert "1 live (1 page) · 1 retired" in html and "Dead Feed" not in html and "All jobs are running." in html
    assert "**" not in html


def test_an_empty_database_still_renders(conn):
    html = ui.page(conn, NOW)
    assert "None open." in html and "Waiting for you" not in html and "Measurements" not in html


def test_decisions_and_password(conn, tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path); fill(c); c.close()
    assert ui.start(lambda: db.connect(path), "", 0) is None                     # no password, no page
    server = ui.start(lambda: db.connect(path), "s3cret", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    auth = {"Authorization": "Basic " + base64.b64encode(b"me:s3cret").decode()}
    try:
        assert urllib.request.urlopen(base + "/health").read() == b"ok"
        for headers in ({}, {"Authorization": "Basic " + base64.b64encode(b"me:wrong").decode()}):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(base + "/", headers=headers))
            assert err.value.code == 401
        assert b"Thalamus" in urllib.request.urlopen(urllib.request.Request(base + "/", headers=auth)).read()
        post = lambda body, extra={}: urllib.request.urlopen(urllib.request.Request(base + "/decide", data=body, headers={**auth, **extra}, method="POST"))
        with pytest.raises(urllib.error.HTTPError) as err:                         # a form posted from another site is refused
            post(b"url=https%3A%2F%2Fais.example%2F&action=approve", {"Origin": "https://evil.example"})
        assert err.value.code == 403
        post(b"url=https%3A%2F%2Fais.example%2F&action=approve"); post(b"url=https%3A%2F%2Fjs.example%2F&action=retry")
        post(b"url=https%3A%2F%2Fjs.example%2F&action=delete")                     # not a decision: ignored
    finally:
        server.shutdown()
    c = db.connect(path)
    assert {r["name"]: r["status"] for r in c.execute("SELECT name, status FROM candidates")} == {"Vessel tracking": "approved", "Scripted": "proposal"}
