"""Pull feeds into the append-only items log. No model calls here."""
from __future__ import annotations

import calendar
import hashlib
import html
import re
import sqlite3
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"<[^>]+>")
MAX_BODY = 1500


def clean(text: str | None) -> str:
    return " ".join(html.unescape(TAG.sub(" ", text or "")).split())


def item_id(url: str, title: str) -> str:
    key = (url or "").split("?utm_")[0].rstrip("/") or title.lower()
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def load_feeds(path: str | Path = ROOT / "config" / "sources.yaml") -> list[dict]:
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    return [f for f in cfg.get("rss_feeds", []) if f.get("url") and f.get("name")]


def parse_feed(source: str, parsed) -> list[dict]:
    """Turn a feedparser result into item rows. Entries without a title are dropped."""
    rows = []
    for e in getattr(parsed, "entries", []) or []:
        title = clean(e.get("title"))
        if not title:
            continue
        url = (e.get("link") or "").strip()
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        rows.append({"id": item_id(url, title), "source": source, "url": url, "title": title,
                     "body": clean(e.get("summary"))[:MAX_BODY],
                     "published_at": float(calendar.timegm(stamp)) if stamp else None})
    return rows


def store(conn: sqlite3.Connection, rows: list[dict], now: float | None = None) -> int:
    """Insert rows that are new. Returns how many were new."""
    now = now or time.time()
    new = 0
    with conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO items(id, source, url, title, body, published_at, fetched_at) VALUES (?,?,?,?,?,?,?)",
                (r["id"], r["source"], r["url"], r["title"], r["body"], r["published_at"], now))
            new += cur.rowcount
    return new


def ingest(conn: sqlite3.Connection, feeds: list[dict] | None = None, fetch=None, now: float | None = None) -> dict:
    """Fetch every feed; one failing feed never stops the rest. Returns {source: new count or error}."""
    if fetch is None:
        import feedparser
        fetch = lambda url: feedparser.parse(url, agent="Mozilla/5.0 (feed reader)")
    from . import sources
    now = now or time.time()
    if feeds is None:
        sources.seed(conn, now)
        feeds = sources.live(conn)
    report = {}
    for f in feeds:
        try:
            rows = parse_feed(f["name"], fetch(f["url"]))
            report[f["name"]] = store(conn, rows, now)
            sources.record_fetch(conn, f["name"], bool(rows), now)     # an empty feed counts as a failure
        except Exception as e:
            report[f["name"]] = f"error: {str(e)[:80]}"
            sources.record_fetch(conn, f["name"], False, now)
    return report
