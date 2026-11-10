"""The long-running service: a small scheduler that keeps its place on disk.

Jobs and when they run (UTC):
  intake    every 4 hours        ingest feeds, fetch full text, extract events
  analyst   daily at 12:00       intake, then one analyst session, then settle and mark
  dream     daily at 03:00       housekeeping and note folding; lessons are rewritten on Sundays
  scout     Sundays at 05:00     look for new sources, retire dead weight
Last-run times live in the database, so a restart neither repeats nor skips a job.
Each job posts a short summary to Discord when a bot token is configured.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
import urllib.request

from . import agent, db, dream, extract, forecasts, ingest, ledger, memory, risk, sources
from . import fetch as fetch_mod
from .broker import get_broker
from .llm import LLM, BudgetExceeded, load_env
from .paths import ROOT
from .prices import YahooProvider
from .wiki import Wiki

HOUR, DAY = 3600.0, 86400.0
DISCORD = "https://discord.com/api/v10"


def last_run(conn: sqlite3.Connection, job: str) -> float:
    r = conn.execute("SELECT v FROM kv WHERE k = ?", (f"last_{job}",)).fetchone()
    return float(r["v"]) if r else 0.0


def mark_run(conn: sqlite3.Connection, job: str, now: float) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (f"last_{job}", str(now)))


def due(conn: sqlite3.Connection, now: float) -> list[str]:
    """Which jobs should run now, in order."""
    t = time.gmtime(now)
    today = now - now % DAY
    ran_today = lambda job: last_run(conn, job) >= today
    out = []
    if t.tm_hour >= 3 and not ran_today("dream"):
        out.append("dream")
    if t.tm_wday == 6 and t.tm_hour >= 5 and not ran_today("scout"):
        out.append("scout")
    if t.tm_hour >= 12 and not ran_today("analyst"):
        out.append("analyst")
    elif now - max(last_run(conn, "intake"), last_run(conn, "analyst")) >= 4 * HOUR:
        out.append("intake")
    return out


def intake(conn, llm, now) -> dict:
    rep = {"new_items": sum(v for v in ingest.ingest(conn, now=now).values() if isinstance(v, int))}
    rep["fulltext"] = fetch_mod.fetch_pending(conn)
    rep["extract"] = extract.extract_pending(conn, llm, now=now)
    forecasts.ensure(conn)
    try:
        db.compact(conn)
    except Exception as e:                  # a busy database is retried at the next intake
        rep["compact"] = f"error: {type(e).__name__}"
    return rep


def run_job(job: str, conn, llm, wiki, provider, broker, now: float) -> str:
    """Run one job and return a short human summary."""
    if job == "intake":
        r = intake(conn, llm, now)
        return f"Intake: {r['new_items']} new items, full text for {r['fulltext']['ok']}, {r['extract']['events']} events kept."
    if job == "analyst":
        i = intake(conn, llm, now)
        a = agent.run_session(conn, llm, wiki, provider, broker, now)
        filled, marks = broker.settle(now), ledger.mark_all(conn, provider, now)
        settled = forecasts.resolve(conn, provider, now=now)
        pf = broker.portfolio(now)
        head = f"Analyst: read {i['new_items']} new items. "
        if a is None:
            return head + "No new events to consider."
        bets = ", ".join(f"{b['side']} {b['symbol']}" + (" (shadow)" if b["conviction"] == 0 else "") for b in a["bets"]) or "none"
        return (head + f"{a['events']} events, {a['fuses_opened']} fuses opened, {a['evidence']} evidence attached, {a['forecasts']} forecasts made "
                f"({settled} settled), bets: {bets}. "
                f"Paper equity ${pf.equity:,.0f}. Spent today ${llm.spent_today(now):.2f}."
                + ("" if a["finished"] else " (Session hit its turn limit; events will be re-read.)")
                + (f"\n\n{a['summary'][:1500]}" if a["summary"] else ""))
    if job == "dream":
        sunday = time.gmtime(now).tm_wday == 6
        d = dream.dream(conn, llm, wiki, provider, broker, do_reflect=sunday, now=now)
        h = d["health"]
        s = d["session"]
        return (f"Dream: wrote {len(s['pages_written'])} pages, merged {s['merged']} duplicate events, "
                f"{d['housekeeping']['fuses_expired']} fuses expired, {d['housekeeping']['forecasts_resolved']} forecasts settled. "
                f"Wiki: {h['pages']} pages, {h['notes_waiting']} notes waiting"
                + (", lessons rewritten." if d["reflected"] else ".")
                + ("" if s["finished"] else " (Session hit its turn limit.)")
                + (f"\n\n{s['summary'][:1200]}" if s["summary"] else ""))
    if job == "scout":
        s = sources.scout(conn, llm, wiki, now)
        return f"Scout: added {len(s['added'])} sources ({', '.join(s['added']) or 'none'}), retired {len(s['retired'])}."
    raise ValueError(f"unknown job {job!r}")


class Discord:
    """Posts plain messages to one channel by name, using the bot token. Silent if no token is set."""

    def __init__(self, token: str | None, channel: str = "thal-logs", send=None):
        self.token, self.channel, self.channel_id, self._send = token, channel, None, send or self._http

    def _http(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(DISCORD + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bot {self.token}", "Content-Type": "application/json",
                                              "User-Agent": "DiscordBot (thalamus, 2.0)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def post(self, text: str) -> bool:
        if not self.token:
            return False
        try:
            if self.channel_id is None:
                for g in self._send("GET", "/users/@me/guilds"):
                    for c in self._send("GET", f"/guilds/{g['id']}/channels"):
                        if c.get("name") == self.channel:
                            self.channel_id = c["id"]
            if self.channel_id is None:
                return False
            for i in range(0, min(len(text), 5700), 1900):          # Discord caps a message at 2000 characters
                self._send("POST", f"/channels/{self.channel_id}/messages", {"content": text[i:i + 1900]})
            return True
        except Exception as e:
            print(f"discord post failed: {type(e).__name__}: {e}", flush=True)
            return False


def load_seed(conn: sqlite3.Connection, wiki: Wiki, now: float) -> bool:
    """On a brand-new database, start from the fuses, notes and lessons shipped in seed/ instead of from nothing."""
    seed_dir = ROOT / "seed"
    if conn.execute("SELECT COUNT(*) FROM fuses").fetchone()[0] or conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
        return False
    if not (seed_dir / "memory.json").exists():
        return False
    data = json.loads((seed_dir / "memory.json").read_text())
    with conn:      # the source list as it stood, including sources the scout found and ones it retired
        for s in data.get("sources", []):
            conn.execute("INSERT OR IGNORE INTO sources(name, url, status, added_at, added_by, why) VALUES (?,?,?,?,?,?)",
                         (s["name"], s["url"], s["status"], now, s["added_by"], s.get("why", "")))
    for f in data.get("fuses", []):
        memory.open_fuse(conn, f["title"], f["thesis"], f["watch"], f.get("expires_days", 90), now=now)
    for n in data.get("notes", []):
        memory.queue_note(conn, n["page"], n["title"], n["kind"], n["note"], now=now)
    for name, text in data.get("pages", {}).items():
        if wiki.read(name) is None:
            wiki.write(name, text["title"], text["kind"], text["body"])
    return True


def tick(conn, llm, wiki, provider, broker, notify, now: float) -> list[str]:
    """Run whatever is due. A failing job is reported and marked as run, so it cannot loop on an error all day."""
    done = []
    for job in due(conn, now):
        try:
            msg = run_job(job, conn, llm, wiki, provider, broker, now)
        except BudgetExceeded as e:
            msg = f"{job.capitalize()} stopped: {e}"
        except Exception as e:
            traceback.print_exc()
            msg = f"{job.capitalize()} failed: {type(e).__name__}: {str(e)[:300]}"
        mark_run(conn, job, now)
        if job == "analyst":
            mark_run(conn, "intake", now)
        print(msg, flush=True)
        if job != "intake" or "failed" in msg:
            notify(msg)
        done.append(job)
    return done


def main() -> None:
    load_env()
    conn, wiki = db.connect(), Wiki()
    llm, provider = LLM(conn), YahooProvider(conn)
    broker = get_broker(conn, provider, risk.load())
    discord = Discord(os.environ.get("DISCORD_BOT_TOKEN"))
    seeded = load_seed(conn, wiki, time.time())
    discord.post(f"**Thalamus v2 online** ({risk.load().mode} mode). " + ("Started from seed memory." if seeded else "Resumed from saved state."))
    print("thalamus v2 serving", flush=True)
    while True:
        tick(conn, llm, wiki, provider, broker, discord.post, time.time())
        time.sleep(60)


if __name__ == "__main__":
    main()
