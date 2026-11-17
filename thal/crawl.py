"""The source crawler: a cheap, daily, mostly model-free hunt for new sources.

Three steps:
  propose  one cheap model call names places worth reading, from the system's focus, the overseer's directions and
           what is already read. Its addresses come from memory and are often wrong, which is fine: nothing is
           trusted until it has been fetched.
  test     plain code. Each candidate is fetched; if it is not itself a feed, the page is searched for one. A
           candidate with no working, recent feed is dead.
  judge    one cheap model call reads sample headlines from every candidate that works and keeps the ones that
           are on the focus and add something. Those join the sources on trial.
Every free candidate is tested for a feed, whatever the model called it. What is left without one (a data service,
a page of notices) and anything paid is never added by the crawler. It is stored as a proposal for a person to
decide on, with what it would add and what it costs if known.
"""
from __future__ import annotations

import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

from . import ingest, sources
from .llm import LLM

DAY = 86400.0
FRESH_DAYS = 30
COMMON_PATHS = ("/feed/", "/rss.xml", "/rss", "/feed.xml", "/atom.xml")
LINK = re.compile(r"<link\b[^>]*>", re.I)
HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)

FOCUS = ("The system trades on physical supply disruption: something real stops or slows the movement or production "
         "of a physical good (a strait, canal, port, river, pipeline, railway, mine, smelter, refinery or crop), and "
         "the effect takes weeks to reach freight rates, inventories and prices.")

PROPOSE = f"""You suggest information sources for a trading system. {FOCUS}

Suggest sources that would report such things first-hand or early and that the system does not read yet: canal, \
port, river, pipeline and rail authorities; coast guards, navies and maritime security centres; marine casualty \
and insurance reports; mine, refinery and plant operators and their regulators; crop, livestock, plant-disease, \
weather and water agencies; customs and export-control offices; specialist trade press; local and non-English \
outlets near chokepoints and producing regions. Give each one's address as best you know it (the feed if you know \
it, otherwise the home or news page). A wrong address costs nothing: every one is fetched and checked by code.

Mark each as:
- feed: a free site that probably has an RSS or Atom feed
- data: a free source of numbers or notices with no feed (river and reservoir levels, rainfall, canal drafts and \
queues, vessel counts, outage lists, a notices page)
- paid: a source that costs money (say roughly what it costs if you know)

Do not repeat anything on the lists you are given. Aim for forty or more, spread across regions and goods."""

JUDGE = f"""You decide which candidate news feeds a trading system should start reading. {FOCUS}

For each candidate you see its address and some recent headlines. Keep it only if its headlines show it regularly \
reports concrete developments on that focus (outages, closures, casualties, restrictions, harvests, disease, \
weather damage, export rules) and it is not a copy of something already read. Drop general news, opinion, \
marketing, company blogs with no operational facts, and anything off the focus. For each one you keep, give a short \
distinct name and one sentence on what it adds."""


class Candidate(BaseModel):
    name: str
    url: str
    kind: Literal["feed", "data", "paid"]
    why: str


class Proposed(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class Verdict(BaseModel):
    url: str
    keep: bool
    name: str = ""
    why: str = ""


class Judged(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


def host(url: str) -> str:
    return (urlparse(url.strip()).hostname or "").lower().removeprefix("www.")


def known_hosts(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT url FROM sources UNION SELECT url FROM candidates").fetchall()
    return {host(r[0]) for r in rows} - {""}


def directions(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT v FROM kv WHERE k = 'overseer_directions'").fetchone()
    return r["v"] if r else ""


def propose(conn: sqlite3.Connection, llm: LLM, now: float) -> int:
    """Ask for candidates and store the ones not seen before. Returns how many were new."""
    have = sorted(known_hosts(conn))
    user = ((f"# What the overseer asked for\n{directions(conn)}\n\n" if directions(conn) else "")
            + "# Sites already read or already considered\n" + ", ".join(have))
    out: Proposed = llm.parse("crawl", PROPOSE, user, Proposed, now)
    new = 0
    with conn:
        for c in out.candidates:
            try:
                url = sources.safe_url(c.url)
            except ValueError:
                continue
            if host(url) in have:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO candidates(url, name, found_at, status, note) VALUES (?,?,?,?,?)",
                               (url, " ".join(c.name.split())[:60], now, "proposal" if c.kind == "paid" else "new",
                                (f"[{c.kind}] " if c.kind != "feed" else "") + c.why.strip()[:400]))
            new += cur.rowcount
            have.append(host(url))
    return new


def feed_links(page: str, base: str) -> list[str]:
    """Feed addresses a page declares in its head, then the usual places a feed lives."""
    out = []
    for tag in LINK.findall(page or ""):
        low = tag.lower()
        if "alternate" in low and ("rss" in low or "atom" in low):
            m = HREF.search(tag)
            if m:
                out.append(urljoin(base, m.group(1)))
    root = "{0.scheme}://{0.netloc}".format(urlparse(base))
    return list(dict.fromkeys(out + [root + p for p in COMMON_PATHS]))[:8]


def find_feed(url: str, fetch, get, now: float) -> tuple[str, list[dict]] | None:
    """The working, recent feed behind an address, with its entries, or None. Never raises."""
    def recent(u):
        try:
            rows = ingest.parse_feed("candidate", fetch(sources.safe_url(u)))
        except Exception:
            return None
        dated = [r["published_at"] for r in rows if r["published_at"]]
        return rows if rows and dated and now - max(dated) <= FRESH_DAYS * DAY else None
    rows = recent(url)
    if rows:
        return url, rows
    try:
        page = get(url)
    except Exception:                       # a refusal, a wall or a dead address: the candidate is simply dropped
        page = ""
    for u in feed_links(page, url):
        rows = recent(u)
        if rows:
            return u, rows
    return None


def test(conn: sqlite3.Connection, fetch, get, now: float, workers: int = 16) -> list[dict]:
    """Fetch every new candidate. Returns the ones with a working feed, each with sample headlines."""
    new = conn.execute("SELECT url, name, note FROM candidates WHERE status = 'new'").fetchall()
    with ThreadPoolExecutor(workers) as pool:
        found = list(pool.map(lambda r: find_feed(r["url"], fetch, get, now), new))
    alive = []
    with conn:
        for r, f in zip(new, found):
            taken = f and conn.execute("SELECT 1 FROM sources WHERE url = ?", (f[0],)).fetchone()
            if not f and r["note"].startswith("[data]"):        # a data source with no feed: a person decides how to wire it in
                conn.execute("UPDATE candidates SET status = 'proposal' WHERE url = ?", (r["url"],))
                continue
            if not f or taken:
                conn.execute("UPDATE candidates SET status = 'dead', note = ? WHERE url = ?",
                             ("already a source" if taken else "no working, recent feed found", r["url"]))
                continue
            conn.execute("UPDATE candidates SET status = 'tested', feed_url = ? WHERE url = ?", (f[0], r["url"]))
            alive.append({"url": r["url"], "feed_url": f[0], "name": r["name"], "titles": [x["title"][:110] for x in f[1][:8]]})
    return alive


def judge(conn: sqlite3.Connection, llm: LLM, alive: list[dict], now: float) -> list[str]:
    """Let the model keep the candidates worth reading; add those as trial sources. Returns the names added."""
    if not alive:
        return []
    user = ("# Already read\n" + ", ".join(r["name"] for r in conn.execute("SELECT name FROM sources WHERE status != 'retired' ORDER BY name"))
            + "\n\n# Candidates\n" + "\n\n".join(f"{c['url']} ({c['name']})\n" + "\n".join(f"- {t}" for t in c["titles"]) for c in alive))
    out: Judged = llm.parse("crawl", JUDGE, user, Judged, now)
    by_url = {c["url"]: c for c in alive}
    added = []
    for v in out.verdicts:
        c = by_url.pop(v.url, None)
        if c is None:
            continue
        name = " ".join((v.name or c["name"]).split())[:60]
        full = conn.execute("SELECT COUNT(*) FROM sources WHERE status != 'retired'").fetchone()[0] >= sources.MAX_LIVE
        clash = conn.execute("SELECT 1 FROM sources WHERE name = ? OR url = ?", (name, c["feed_url"])).fetchone()
        ok = v.keep and name and not full and not clash
        with conn:
            if ok:
                conn.execute("INSERT INTO sources(name, url, status, added_at, added_by, why) VALUES (?,?,'trial',?,'crawl',?)",
                             (name, c["feed_url"], now, v.why.strip()[:400]))
                added.append(name)
            conn.execute("UPDATE candidates SET status = ?, note = ? WHERE url = ?",
                         ("added" if ok else "rejected", (v.why.strip() or ("source list is full" if full else "not kept"))[:400], c["url"]))
    with conn:                               # anything the model did not mention is left for the next run to judge
        for url in by_url:
            conn.execute("UPDATE candidates SET status = 'new' WHERE url = ?", (url,))
    return added


def proposals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Sources the crawler cannot add by itself (data services, notice pages, paid products), awaiting a decision."""
    return conn.execute("SELECT name, url, note, found_at FROM candidates WHERE status = 'proposal' ORDER BY found_at DESC").fetchall()


def crawl(conn: sqlite3.Connection, llm: LLM, now: float | None = None, fetch=None, get=None) -> dict:
    now = now or time.time()
    if get is None:
        from .fetch import http_get
        get = lambda url: http_get(url)[1]
    if fetch is None:                       # through the same fetcher as pages, so every request has a timeout
        import feedparser
        fetch = lambda url: feedparser.parse(get(url))
    before = len(proposals(conn))
    proposed = propose(conn, llm, now)
    alive = test(conn, fetch, get, now)
    added = judge(conn, llm, alive, now)
    return {"proposed": proposed, "working": len(alive), "added": added, "new_proposals": len(proposals(conn)) - before}
