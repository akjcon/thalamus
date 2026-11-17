"""The control page: one private web page that shows what the system is doing.

It is served by the same process as the scheduler, from a second thread with its own database connection. With
THAL_UI_PASSWORD set it asks for that password and the owner can decide on waiting sources. With THAL_UI_OPEN=yes
and no password it is open to anyone who has the address, and read-only: no buttons, and decisions are refused.
With neither there is no page. It reads the same tables everything
else writes, and its only writes are the owner's decisions on sources that are waiting (approve, decline, retry).
Everything shown comes from the database and is escaped: source names, headlines and model text are untrusted.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import ingest, lab, ledger, oversee

HOUR, DAY = 3600.0, 86400.0
DECISIONS = {"approve": "approved", "decline": "declined", "retry": "proposal"}
JOBS = ("intake", "analyst", "dream", "crawl", "lab", "overseer", "scout", "report")
WHAT = {"intake": "reads every source, extracts events", "analyst": "reads events, makes forecasts and bets",
        "dream": "folds notes into long-term memory", "crawl": "finds and wires in new sources",
        "lab": "tests trading rules, runs the ones that passed", "overseer": "checks every part, directs the crawler", "scout": "weekly source search and retirement",
        "report": "weekly scorecard"}

CSS = """
:root{--bg:#fafaf8;--fg:#1c1c1a;--dim:#77756f;--line:#e4e2dc;--card:#fff;--ok:#2e7d4f;--bad:#c0392b;--warn:#b7791f;--run:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#e9e7e1;--dim:#8f8d86;--line:#2b2a27;--card:#1c1c1a;--ok:#5cbf85;--bad:#ef6b5b;--warn:#e0a94a;--run:#7aa7ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:8px 28px;align-items:baseline;margin-bottom:18px}
h1{font-size:18px;margin:0;font-weight:650;letter-spacing:-.01em}h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:28px 0 8px;font-weight:600}
.stat b{font-weight:650;font-variant-numeric:tabular-nums}.stat{color:var(--dim)}.stat b{color:var(--fg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.dim{color:var(--dim)}.nw{white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--dim)}
.ok .dot{background:var(--ok)}.failed .dot,.overdue .dot{background:var(--bad)}.running .dot{background:var(--run);animation:p 1.2s infinite}
@keyframes p{50%{opacity:.25}}.pos{color:var(--ok)}.neg{color:var(--bad)}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}
pre{white-space:pre-wrap;margin:8px 0 2px;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg)}
.pad{padding:12px 14px}.prose{white-space:pre-wrap}.tag{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 5px;color:var(--dim);white-space:nowrap}
button{font:inherit;font-size:12px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:5px;padding:2px 9px;cursor:pointer;margin-left:4px}
button:hover{border-color:var(--fg)}input[type=search]{font:inherit;width:240px;max-width:100%;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}.tile{padding:11px 13px}.tile b{font-size:17px;font-variant-numeric:tabular-nums}
svg{display:block;margin-top:6px}a{color:inherit}
"""

JS = """
const q=document.getElementById('q');if(q)q.oninput=()=>{const v=q.value.toLowerCase();
document.querySelectorAll('#src tbody tr').forEach(r=>r.style.display=r.textContent.toLowerCase().includes(v)?'':'none')};
"""


def e(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def ago(then: float | None, now: float) -> str:
    if not then:
        return "never"
    s = max(0, now - then)
    return f"{s / 60:.0f}m ago" if s < HOUR else f"{s / HOUR:.0f}h ago" if s < 2 * DAY else f"{s / DAY:.0f}d ago"


def pct(v) -> str:
    return "" if v is None else f'<span class="{"pos" if v > 0 else "neg" if v < 0 else ""}">{v * 100:+.1f}%</span>'


def spark(values: list[float], w: int = 224, h: int = 30) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = " ".join(f"{i * w / (len(values) - 1):.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(values))
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'


def job_state(conn: sqlite3.Connection, job: str, running: str, now: float) -> tuple[str, float, str]:
    last = oversee._last(conn, job)
    row = conn.execute("SELECT ok, summary FROM job_log WHERE job = ? ORDER BY id DESC LIMIT 1", (job,)).fetchone()
    if running == job:
        state = "running"
    elif row and not row["ok"]:
        state = "failed"
    elif last and now - last > oversee.EXPECTED[job]:
        state = "overdue"
    else:
        state = "ok" if last else "not yet run"
    return state, last, (row["summary"] if row else "")


def page(conn: sqlite3.Connection, now: float | None = None, decisions: bool = True) -> str:
    now = now or time.time()
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    week, day = now - 7 * DAY, now - DAY
    kv = {r["k"]: r["v"] for r in q("SELECT k, v FROM kv WHERE k IN ('running_job', 'overseer_directions')")}
    running = (kv.get("running_job") or "|").split("|")[0]
    out = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Thalamus</title><style>{CSS}</style><main>"]

    # ---- header
    cost = lambda since: one("SELECT COALESCE(SUM(cost), 0) FROM runs WHERE at >= ?", since)
    live = one("SELECT COUNT(*) FROM sources WHERE status != 'retired'")
    out.append("<header><h1>Thalamus</h1>"
               f"<span class=stat>spent today <b>${cost(now - now % DAY):.2f}</b></span><span class=stat>7 days <b>${cost(week):.2f}</b></span>"
               f"<span class=stat>sources <b>{live}</b></span>"
               f"<span class=stat>events 24h <b>{one('SELECT COUNT(*) FROM events e JOIN items i ON i.id = e.item_id WHERE i.fetched_at >= ?', day)}</b></span>"
               f"<span class=stat>open bets <b>{one("SELECT COUNT(*) FROM bets WHERE closed_at IS NULL AND source IN ('v2', 'lab')")}</b></span>"
               f"<span class=stat dim>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}</span></header>")

    # ---- agents
    spend = {r["kind"]: r["c"] for r in q("SELECT kind, SUM(cost) c FROM runs WHERE at >= ? GROUP BY kind", week)}
    spend["intake"], spend["crawl"] = spend.get("extract", 0), spend.get("crawl", 0) + spend.get("build", 0)
    spend["dream"] = spend.get("dream", 0) + spend.get("reflect", 0)
    rows = []
    for job in JOBS:
        state, last, summary = job_state(conn, job, running, now)
        summary = summary.replace("**", "")
        head, _, rest = summary.partition("\n")
        said = (f"<details><summary>{e(head[:150])}{' …' if rest or len(head) > 150 else ''}</summary><pre>{e(summary)}</pre></details>"
                if summary else "")
        rows.append(f"<tr class='{e(state.split()[0])}'><td class=nw><span class=dot></span><b>{job}</b></td><td class=nw>{e(state)}</td>"
                    f"<td class='nw dim'>{ago(last, now)}</td><td class=n>${spend.get(job, 0):.2f}</td><td class=dim>{WHAT[job]}</td><td>{said}</td></tr>")
    out.append("<h2>Agents</h2><div class=card><table><tr><th>Job<th>State<th>Last run<th class=n>7d cost<th>Does<th>Last result</tr>"
               + "".join(rows) + "</table></div>")

    # ---- overseer
    last = conn.execute("SELECT at, summary FROM job_log WHERE job = 'overseer' ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        out.append(f"<h2>Overseer · {ago(last['at'], now)}</h2><div class='card pad prose'>{e(last['summary'].replace('**', ''))}</div>")

    # ---- waiting for the owner: paid sources, and free ones the builder could not wire in
    waiting = q("SELECT url, name, note, status FROM candidates WHERE status = 'needs_work' OR (status = 'proposal' AND note LIKE '[paid]%') "
                "ORDER BY (note LIKE '[paid]%') DESC, found_at DESC")
    queued = one("SELECT COUNT(*) FROM candidates WHERE status = 'proposal' AND note NOT LIKE '[paid]%'")
    if waiting or queued:
        rows = []
        for w in waiting:
            paid = w["note"].startswith("[paid]")
            note = w["note"].removeprefix("[paid]").removeprefix("[data]").strip()
            acts = "" if not decisions else (("<button name=action value=approve>Approve</button>" if paid else "<button name=action value=retry>Retry</button>")
                                             + "<button name=action value=decline>Decline</button>")
            rows.append(f"<tr><td><a href='{e(w['url'])}' rel=noreferrer target=_blank>{e(w['name'])}</a><br><span class=tag>{'paid' if paid else 'needs work'}</span></td>"
                        f"<td class=dim>{e(note)}</td><td class=nw><form method=post action=decide><input type=hidden name=url value='{e(w['url'])}'>{acts}</form></td></tr>")
        out.append(f"<h2>Waiting for you · {len(waiting)}</h2>"
                   + (f"<div class=dim style='margin-bottom:8px'>{queued} more are queued for the builder's next run.</div>" if queued else "")
                   + ("<div class=card><table>" + "".join(rows) + "</table></div>" if rows else ""))

    # ---- measurements
    tiles = []
    for s in q("SELECT name FROM sources WHERE status != 'retired' AND kind NOT IN ('rss', 'page', 'kalshi') ORDER BY name"):   # by source: the table also holds market prices
        hist = q("SELECT value, unit, day FROM readings WHERE source = ? ORDER BY day DESC LIMIT 90", s["name"])[::-1]
        if not hist:
            continue
        values = [r["value"] for r in hist]
        tiles.append(f"<div class='card tile'><div class=dim>{e(s['name'])}</div><b>{values[-1]:g}</b> {e(hist[-1]['unit'])} "
                     f"<span class=dim>· {e(hist[-1]['day'])} · {len(values)}d range {min(values):g}–{max(values):g}</span>{spark(values)}</div>")
    priced = max(one("SELECT COUNT(*) FROM readings WHERE day = ? AND source LIKE 'kalshi:%'", time.strftime("%Y-%m-%d", time.gmtime(t))) for t in (now, day))
    if tiles or priced:
        out.append("<h2>Measurements</h2>" + (f"<div class=dim style='margin-bottom:8px'>Plus the daily price of {priced} prediction-market contracts.</div>" if priced else "")
                   + "<div class=grid>" + "".join(tiles) + "</div>")

    # ---- bets
    fuses = {r["id"]: r["title"] for r in q("SELECT id, title FROM fuses")}
    rows = [f"<tr><td class=nw><b>{e(b['side'])} {e(b['symbol'])}</b> <span class=tag>{'lab rule' if b['source'] == 'lab' else 'shadow' if b['kind'] == 'shadow' else 'paper'}</span></td>"
            f"<td>{e(fuses.get(b['fuse_id'], b['thesis'][:110]))}</td><td class='n dim'>{(now - b['created_at']) / DAY:.0f}d of {b['horizon_days'] or '?'}</td>"
            f"<td class=n>{pct(b['ret'])}</td><td class=n>{pct(b['vs_bench'])} <span class=dim>{e(b['benchmark'] or '')}</span></td></tr>"
            for b in ledger.open_bets(conn) + ledger.open_bets(conn, "lab")]
    board = {r["horizon"]: r for r in ledger.scoreboard(conn, by="source", unique=False, where="b.source = 'v2'")}
    record = " · ".join(f"{h}: {pct(board[h]['vs_bench']) or 'n/a'} vs sector (n={board[h]['n']})" for h in ("1w", "4w", "12w") if h in board)
    out.append(f"<h2>Open bets · {len(rows)}</h2>" + (f"<div class=dim style='margin-bottom:8px'>All bets so far — {record}</div>" if record else "")
               + ("<div class=card><table><tr><th>Bet<th>Thesis<th class=n>Held<th class=n>Return<th class=n>vs sector</tr>" + "".join(rows) + "</table></div>"
                  if rows else "<div class='card pad dim'>None open.</div>"))

    # ---- strategy lab
    rules = q("SELECT * FROM strategies WHERE status != 'invalid' ORDER BY (status = 'live') DESC, (status = 'passed_dev') DESC, id DESC LIMIT 40")
    if rules:
        label = {"passed_dev": "waiting for holdout", "failed_dev": "failed development", "failed_holdout": "failed holdout"}
        res = lambda r: f"{pct(r['mean'])} <span class=dim>n={r['n']}" + (f", t {r['t']:g}" if "t" in r else "") + (f", placebo {r['p']:.2%}" if "p" in r else "") + "</span>" if r and r["n"] else ""
        rows = []
        for s in rules:
          try:
            dev, held, live_rec = (json.loads(s["dev"]) if s["dev"] else None), (json.loads(s["holdout"]) if s["holdout"] else None), lab.live_record(conn, s["id"])
            rows.append(f"<tr><td><b title='{e(s['hypothesis'])}'>{e(s['name'])}</b><br><span class=dim>{e(lab.in_words(s['spec']))}</span></td>"
                        f"<td class=nw><span class=tag>{e(label.get(s['status'], s['status']))}</span></td>"
                        f"<td class=n>{res(dev)}</td><td class=n>{res(held)}</td><td class=n>{res(live_rec)}</td></tr>")
          except Exception:                 # one unreadable rule must not take the page down
            rows.append(f"<tr><td colspan=5 class=dim>rule #{s['id']} could not be shown</td></tr>")
        tried = one("SELECT COUNT(*) FROM strategies WHERE status != 'invalid'")
        out.append(f"<h2>Strategy lab · {tried} rules tried</h2><div class=dim style='margin-bottom:8px'>Mean return per trade over the benchmark, after costs. "
                   f"To pass development, at most {lab.bar(tried + 1):.2%} of the next rule's placebo versions may do as well.</div>"
                   "<div class=card><table><tr><th>Rule<th>Status<th class=n>Development<th class=n>Holdout<th class=n>Live</tr>" + "".join(rows) + "</table></div>")

    # ---- what the sources are producing
    top = q("SELECT e.magnitude m, e.summary s, i.source src, i.fetched_at t FROM events e JOIN items i ON i.id = e.item_id "
            "WHERE i.fetched_at >= ? AND e.dup_of IS NULL ORDER BY e.magnitude DESC, e.novelty DESC, i.fetched_at DESC LIMIT 12", day)
    if top:
        out.append("<h2>Biggest events, last 24h</h2><div class=card><table>" + "".join(
            f"<tr><td class=n>{r['m']}</td><td>{e(r['s'][:260])}</td><td class='dim nw'>{e(r['src'][:28])}</td><td class='dim nw'>{ago(r['t'], now)}</td></tr>"
            for r in top) + "</table></div>")

    # ---- sources
    items = {r["source"]: (r["d"], r["w"]) for r in q("SELECT source, SUM(fetched_at >= ?) d, COUNT(*) w FROM items WHERE fetched_at >= ? GROUP BY source", day, week)}
    events = {r["source"]: (r["d"], r["w"]) for r in q("SELECT i.source, SUM(i.fetched_at >= ?) d, COUNT(*) w FROM events e JOIN items i ON i.id = e.item_id "
                                                       "WHERE i.fetched_at >= ? GROUP BY i.source", day, week)}
    srcs = sorted(q("SELECT * FROM sources WHERE status != 'retired'"), key=lambda s: (-events.get(s["name"], (0, 0))[1], -items.get(s["name"], (0, 0))[1], s["name"]))
    shown = lambda kind: "series" if kind in ingest.SERIES else "markets" if kind == "kalshi" else kind   # the named gauge readers are series too
    count: dict[str, int] = {}
    for s in srcs:
        count[shown(s["kind"])] = count.get(shown(s["kind"]), 0) + 1
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(count.items(), key=lambda kv: -kv[1]))
    retired = one("SELECT COUNT(*) FROM sources WHERE status = 'retired'")
    rows = []
    for s in srcs:
        it, ev = items.get(s["name"], (0, 0)), events.get(s["name"], (0, 0))
        health = f"<span class=neg>{s['fails']} failed</span>" if s["fails"] else ago(s["last_ok_at"], now)
        rows.append(f"<tr><td><a href='{e(s['url'])}' rel=noreferrer target=_blank title='{e(s['why'])}'>{e(s['name'])}</a></td><td><span class=tag>{e(shown(s['kind']))}</span></td>"
                    f"<td class=dim>{e(s['status'])}</td><td class=dim>{e(s['added_by'])}</td><td class=n>{it[0] or ''}</td><td class=n>{ev[0] or ''}</td>"
                    f"<td class=n>{it[1] or ''}</td><td class=n>{ev[1] or ''}</td><td class='dim nw'>{health}</td></tr>")
    out.append(f"<h2>Sources · {len(srcs)} live ({e(kinds)}) · {retired} retired</h2>"
               "<input id=q type=search placeholder='Filter sources'><div class=card><table id=src><thead><tr><th>Source<th>Kind<th>Status<th>Added by"
               "<th class=n>Items 24h<th class=n>Events 24h<th class=n>Items 7d<th class=n>Events 7d<th>Last read</tr></thead><tbody>"
               + "".join(rows) + "</tbody></table></div>")
    out.append(f"</main><script>{JS}</script></html>")
    return "".join(out)


def decide(conn: sqlite3.Connection, url: str, action: str) -> bool:
    """Record the owner's decision on a waiting source. Retry sends it back to the builder's queue."""
    if action not in DECISIONS:
        return False
    with conn:
        cur = conn.execute("UPDATE candidates SET status = ? WHERE url = ? AND status IN ('proposal', 'needs_work')", (DECISIONS[action], url))
    return bool(cur.rowcount)


def authorised(header: str | None, password: str) -> bool:
    try:
        kind, _, token = (header or "").partition(" ")
        given = base64.b64decode(token).decode().partition(":")[2] if kind.lower() == "basic" else ""
    except Exception:
        return False
    return bool(password) and hmac.compare_digest(given.encode(), password.encode())


def make_handler(connect, password: str | None, decisions: bool = True):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str = "", headers: dict | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            for k, v in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(data)), "Cache-Control": "no-store",
                         "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if password is None or authorised(self.headers.get("Authorization"), password):   # None: local viewing on this machine only
                return True
            self._send(401, "Password required.", {"WWW-Authenticate": 'Basic realm="Thalamus"'})
            return False

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":                       # for the platform's liveness check; says nothing about the system
                return self._send(200, "ok")
            if not self._allowed():
                return
            if path != "/":
                return self._send(404, "Not found.")
            conn = connect()
            try:
                self._send(200, page(conn, decisions=decisions))
            finally:
                conn.close()

        def do_POST(self):
            if not self._allowed():
                return
            if not decisions:
                return self._send(403, "This page is read-only.")
            origin = self.headers.get("Origin")
            if urlparse(self.path).path != "/decide" or (origin and urlparse(origin).netloc != self.headers.get("Host")):
                return self._send(403, "Refused.")
            form = parse_qs(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10_000)).decode())
            conn = connect()
            try:
                decide(conn, (form.get("url") or [""])[0], (form.get("action") or [""])[0])
            finally:
                conn.close()
            self._send(303, "", {"Location": "/"})

        def log_message(self, *args):                   # the scheduler's log stays readable
            pass
    return Handler


def start(connect, password: str | None = None, port: int | None = None, open_page: bool | None = None) -> ThreadingHTTPServer | None:
    """Serve the page from a background thread: behind the password if one is set, else open and read-only if
    THAL_UI_OPEN=yes, else not at all (returns None)."""
    password = password if password is not None else os.environ.get("THAL_UI_PASSWORD", "")
    open_page = open_page if open_page is not None else os.environ.get("THAL_UI_OPEN", "").lower() == "yes"
    port = port if port is not None else int(os.environ.get("PORT", "8080"))
    if password:
        return _serve(connect, password, "0.0.0.0", port)
    return _serve(connect, None, "0.0.0.0", port, decisions=False) if open_page else None


def start_local(connect, port: int = 8787) -> ThreadingHTTPServer:
    """For looking at a database on your own machine: no password, reachable only from this machine."""
    return _serve(connect, None, "127.0.0.1", port)


def _serve(connect, password: str | None, host: str, port: int, decisions: bool = True) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(connect, password, decisions))
    threading.Thread(target=server.serve_forever, daemon=True, name="ui").start()
    return server
