"""Pull every source into the append-only items log. No model calls here.

A source has a kind. Whatever the kind, what comes out is the same: dated items, which the rest of the system reads.
  rss      a news feed; one item per entry
  series   a measurement that changes over time (a river level today; rainfall, canal drafts, freight rates later).
           Each day's value is kept in the readings table, and one item a day states the level and how it has moved,
           so a measurement reaches the analyst the same way a headline does.
To read a new kind of data, add a reader to SERIES (or a new kind beside it) and list the source in config/sources.yaml.
"""
from __future__ import annotations

import calendar
import hashlib
import html
import json
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
    """Every configured source: news feeds (kind rss) and data feeds (their own kind)."""
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    feeds = [{**f, "kind": "rss"} for f in cfg.get("rss_feeds", [])] + [f for f in cfg.get("data_feeds", []) if f.get("kind") in SERIES]
    return [f for f in feeds if f.get("url") and f.get("name")]


def _pegelonline(text: str) -> list[tuple[float, float]]:
    """German federal waterways gauge service: [{"timestamp": ISO, "value": cm}, ...]."""
    from datetime import datetime
    return [(datetime.fromisoformat(r["timestamp"]).timestamp(), float(r["value"])) for r in json.loads(text) if r.get("value") is not None]


def _usgs(text: str) -> list[tuple[float, float]]:
    """US Geological Survey water services, daily values for one site and one parameter."""
    from datetime import datetime
    out = []
    for series in json.loads(text)["value"]["timeSeries"]:
        for v in series["values"][0]["value"]:
            if float(v["value"]) > -999:        # the service's marker for a missing value
                out.append((datetime.fromisoformat(v["dateTime"]).replace(tzinfo=None).timestamp() - time.timezone, float(v["value"])))
    return out


SERIES = {"pegelonline": (_pegelonline, "cm"), "usgs_gauge": (_usgs, "ft")}     # kind -> (reader, unit)
WEEK = 7


def read_series(conn: sqlite3.Connection, feed: dict, text: str) -> list[dict]:
    """Store each day's latest measurement, and return one item for the newest day saying where the level is and
    how it has moved. The item's id is the source and the day, so it is written once per day."""
    reader, unit = SERIES[feed["kind"]]
    days: dict[str, tuple[float, float]] = {}
    for at, value in sorted(reader(text)):
        days[time.strftime("%Y-%m-%d", time.gmtime(at))] = (at, value)
    if not days:
        return []
    with conn:
        for day, (at, value) in days.items():
            conn.execute("INSERT OR REPLACE INTO readings(source, day, at, value, unit) VALUES (?,?,?,?,?)", (feed["name"], day, at, value, unit))
    hist = conn.execute("SELECT day, at, value FROM readings WHERE source = ? ORDER BY day", (feed["name"],)).fetchall()
    last = hist[-1]
    title = f"{feed['name']}: {last['value']:g} {unit} on {last['day']}"
    if len(hist) > 1:
        ref = hist[max(0, len(hist) - 1 - WEEK)]
        title += f", {last['value'] - ref['value']:+g} {unit} since {ref['day']}"
    values = [r["value"] for r in hist]
    body = (f"Measured level, not news. Over the {len(hist)} days on record ({hist[0]['day']} to {last['day']}) it ranged from "
            f"{min(values):g} to {max(values):g} {unit}." + (" This is the lowest on record here." if len(hist) >= 30 and last["value"] == min(values) else "")
            + (" This is the highest on record here." if len(hist) >= 30 and last["value"] == max(values) else "")
            + (f" {feed['note']}" if feed.get("note") else ""))
    return [{"id": item_id("", f"{feed['name']} {last['day']}"), "source": feed["name"], "url": feed["url"], "title": title,
             "body": body[:MAX_BODY], "published_at": last["at"]}]


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


def ingest(conn: sqlite3.Connection, feeds: list[dict] | None = None, fetch=None, now: float | None = None, get=None) -> dict:
    """Fetch every source; one failing source never stops the rest. Returns {source: new count or error}.
    fetch reads a news feed; get returns the text at a URL for a data feed."""
    if fetch is None:
        import feedparser
        fetch = lambda url: feedparser.parse(url, agent="Mozilla/5.0 (feed reader)")
    from . import sources
    now = now or time.time()
    if feeds is None:
        sources.seed(conn, now)
        feeds = sources.live(conn)
    report = {}
    notes = {c["name"]: c for c in load_feeds() if c["kind"] != "rss"}      # a data feed's note lives in the config
    for f in feeds:
        try:
            if (f.get("kind") or "rss") == "rss":
                rows = parse_feed(f["name"], fetch(f["url"]))
            else:
                if get is None:
                    from .fetch import http_get
                    get = lambda url: http_get(url)[1]
                rows = read_series(conn, {**notes.get(f["name"], {}), **f}, get(f["url"]))
            report[f["name"]] = store(conn, rows, now)
            sources.record_fetch(conn, f["name"], bool(rows), now)     # an empty feed counts as a failure
        except Exception as e:
            report[f["name"]] = f"error: {str(e)[:80]}"
            sources.record_fetch(conn, f["name"], False, now)
    return report
