"""Pull every source into the append-only items log. No model calls here.

A source has a kind. Whatever the kind, what comes out is the same: dated items, which the rest of the system reads.
  rss      a news feed; one item per entry
  page     a web page with no feed (notices to shipping, an outage list). The lines not seen on it before become
           one item, so a new notice reaches the analyst the day it is posted
  series   a measurement that changes over time (a river level, a canal draft, rainfall, a queue of ships). Each
           day's value is kept in the readings table, and one item a day states the level and how it has moved
  kalshi   prediction-market prices. Each quoted contract's price is kept daily in readings (by forecasts), and a
           contract whose price moved sharply since the day before becomes an item
A page or series source is read by a recipe: a small piece of data saying where the text or the number is (see
RECIPE). A recipe is data, not code, so the source builder can write one for a source it has just found, have it
checked by check_recipe, and switch it on without a deploy. The two named readers in SERIES predate recipes.
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
    feeds = [{**f, "kind": "rss"} for f in cfg.get("rss_feeds", [])] + [f for f in cfg.get("data_feeds", []) if f.get("kind") in KINDS]
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
KINDS = ("rss", "page", "series", "kalshi") + tuple(SERIES)
WEEK = 7
MAX_PATTERN, MAX_SCAN = 300, 200_000

RECIPE = """A recipe is a JSON object.
For a page (kind "page"), all fields optional:
  {"after": "text that comes just before the part worth watching", "before": "text that comes just after it"}
For a series (kind "series"), one of:
  {"format": "json", "rows": "dotted.path.to.the.list ('' if the document is the list; numbers index into lists)",
   "time": "field of each row holding its time (dotted path)", "value": "field holding the number", "unit": "cm"}
  {"format": "csv", "time": "name of the time column", "value": "name of the value column", "unit": "mm",
   "delimiter": ","}
  {"format": "regex", "pattern": "regular expression with ONE group capturing the current number in the page text",
   "unit": "ft"}                    (the reading is dated when it is fetched)
Optional for any series:
  "min" and "max"      values outside are dropped as errors or missing-data markers
  "map"                text that stands for a number, e.g. {"T": 0} where T means a trace of rain
  "sum": true          the value is an amount per day that adds up (rainfall, arrivals), not a level; the daily item
                       then reports totals over 7 and 30 days and the month so far
  "history"            an address returning the same rows over many past years; it is read once, when the source is new
An address may carry dates: {today} and {days_ago:N} are replaced with YYYY-MM-DD dates when it is fetched."""


def expand(url: str, now: float) -> str:
    """Fill in the date placeholders an address may carry, so a data service can be asked for a recent window."""
    day = lambda n: time.strftime("%Y-%m-%d", time.gmtime(now - n * 86400))
    return re.sub(r"\{days_ago:(\d{1,5})\}", lambda m: day(int(m.group(1))), url.replace("{today}", day(0)))


def _dig(obj, path: str):
    for key in [k for k in str(path).split(".") if k != ""]:
        obj = obj[int(key)] if isinstance(obj, list) else obj[key]
    return obj


def _when(v) -> float:
    """A timestamp from epoch seconds, epoch milliseconds, or an ISO or common date string. Naive times are UTC."""
    from datetime import datetime, timezone
    if isinstance(v, (int, float)) or str(v).strip().replace(".", "", 1).isdigit():
        x = float(v)
        return x / 1000 if x > 1e11 else x
    text = str(v).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        d = next(d for d in (_try(text, f) for f in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y",
                                                      "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y%m%d", "%d %b %Y", "%b %d, %Y")) if d)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _try(text: str, fmt: str):
    from datetime import datetime
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


def _number(v) -> float:
    return float(str(v).replace(",", "").strip()) if not isinstance(v, (int, float)) else float(v)


def recipe_readings(recipe: dict, text: str, now: float) -> list[tuple[float, float]]:
    """(time, value) pairs a series recipe finds in the fetched text. Rows it cannot read are skipped."""
    import csv, io, math
    fmt, out, means = recipe.get("format"), [], recipe.get("map") or {}
    num = lambda v: _number(means.get(v.strip(), v) if isinstance(v, str) else v)
    if fmt == "json":
        for row in _dig(json.loads(text), recipe.get("rows", "")):
            try:
                out.append((_when(_dig(row, recipe["time"])), num(_dig(row, recipe["value"]))))
            except (KeyError, IndexError, TypeError, ValueError, StopIteration):
                continue
    elif fmt == "csv":
        for row in csv.DictReader(io.StringIO(text), delimiter=recipe.get("delimiter") or ","):
            try:
                out.append((_when(row[recipe["time"]]), num(row[recipe["value"]])))
            except (KeyError, TypeError, ValueError, StopIteration):
                continue
    elif fmt == "regex":
        pattern = str(recipe.get("pattern", ""))
        if not pattern or len(pattern) > MAX_PATTERN:
            raise ValueError("a regex recipe needs a pattern of at most %d characters" % MAX_PATTERN)
        from .fetch import plain
        m = re.search(pattern, plain(text)[:MAX_SCAN] if "<" in text[:2000] else text[:MAX_SCAN], re.I | re.S)
        if m and m.groups():
            out.append((now, num(m.group(1))))
    else:
        raise ValueError(f"unknown series format {fmt!r}")
    lo, hi = recipe.get("min"), recipe.get("max")
    return [(at, v) for at, v in out if math.isfinite(v) and (lo is None or v >= float(lo)) and (hi is None or v <= float(hi))]


def page_lines(recipe: dict, html: str) -> list[str]:
    """The lines of visible text on a page worth comparing from one visit to the next."""
    from .fetch import plain
    text = plain(html)
    for key, keep_after in (("after", True), ("before", False)):
        mark = str(recipe.get(key) or "")
        i = text.find(mark) if mark else -1
        if i >= 0:
            text = text[i + len(mark):] if keep_after else text[:i]
    return list(dict.fromkeys(l for l in text.splitlines() if len(l) >= 25))


def read_page(conn: sqlite3.Connection, feed: dict, html: str, now: float) -> tuple[list[dict], bool]:
    """(items, whether the page was readable). The first visit only remembers what is there. After that, the lines
    not seen before become one item. Lines are remembered, so a notice that scrolls off and returns is not new."""
    lines = page_lines(feed.get("recipe") or {}, html)
    hashes = {hashlib.sha256(l.lower().encode()).hexdigest()[:16]: l for l in lines}
    seen = {r[0] for r in conn.execute("SELECT h FROM page_seen WHERE source = ?", (feed["name"],))}
    fresh = [l for h, l in hashes.items() if h not in seen]
    with conn:
        conn.executemany("INSERT OR IGNORE INTO page_seen(source, h) VALUES (?, ?)", [(feed["name"], h) for h in hashes])
    if not seen or not fresh:
        return [], bool(lines)
    body = "New on this page since the last visit:\n" + "\n".join(fresh)
    return [{"id": item_id("", feed["name"] + "|" + "|".join(sorted(h for h, l in hashes.items() if l in fresh))), "source": feed["name"],
             "url": feed["url"], "title": f"{feed['name']}: {fresh[0][:140]}", "body": body[:MAX_BODY], "published_at": now}], True


def check_recipe(kind: str, recipe: dict, text: str, now: float) -> str:
    """Why a recipe does not work on this fetched text, or '' if it does. This is the gate a source the builder
    wrote must pass before it is switched on."""
    try:
        if kind == "page":
            n = len(page_lines(recipe, text))
            return "" if n >= 3 else f"only {n} lines of text found; the page may be empty, scripted or walled"
        if kind != "series":
            return f"unknown kind {kind!r}"
        got = recipe_readings(recipe, text, now)
        if not got:
            return "the recipe found no readings in the fetched text"
        newest = max(at for at, _ in got)
        if newest > now + 2 * 86400 or newest < now - 120 * 86400:
            return f"the newest reading is dated {time.strftime('%Y-%m-%d', time.gmtime(newest))}, which is not recent; the time field is probably wrong"
        if not str(recipe.get("unit") or "").strip():
            return "a series recipe must state its unit"
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:200]}"


def read_series(conn: sqlite3.Connection, feed: dict, text: str, now: float | None = None) -> list[dict]:
    """Store each day's latest measurement, and return one item for the newest day saying where the level is and
    how it has moved. The item's id is the source and the day, so it is written once per day."""
    now = now or time.time()
    if feed["kind"] in SERIES:
        reader, unit = SERIES[feed["kind"]]
        found = reader(text)
    else:
        recipe = feed.get("recipe") or {}
        found, unit = recipe_readings(recipe, text, now), str(recipe.get("unit") or "")
    days: dict[str, tuple[float, float]] = {}
    for at, value in sorted(found):
        days[time.strftime("%Y-%m-%d", time.gmtime(at))] = (at, value)
    if not days:
        return []
    with conn:
        for day, (at, value) in days.items():
            conn.execute("INSERT OR REPLACE INTO readings(source, day, at, value, unit) VALUES (?,?,?,?,?)", (feed["name"], day, at, value, unit))
    hist = conn.execute("SELECT day, at, value FROM readings WHERE source = ? ORDER BY day", (feed["name"],)).fetchall()
    last = hist[-1]
    title = f"{feed['name']}: {last['value']:g} {unit} on {last['day']}"
    note = feed.get("note") or feed.get("why") or ""
    if feed["kind"] not in SERIES and (feed.get("recipe") or {}).get("sum"):
        more, body = _totals(hist, unit)
        title += more
    else:
        if len(hist) > 1:
            ref = hist[max(0, len(hist) - 1 - WEEK)]
            title += f", {last['value'] - ref['value']:+g} {unit} since {ref['day']}"
        values = [r["value"] for r in hist]
        body = (f"Measured value, not news. Over the {len(hist)} days on record ({hist[0]['day']} to {last['day']}) it ranged from "
                f"{min(values):g} to {max(values):g} {unit}." + (" This is the lowest on record here." if len(hist) >= 30 and last["value"] == min(values) else "")
                + (" This is the highest on record here." if len(hist) >= 30 and last["value"] == max(values) else ""))
    # No link: the address is a data service, and its raw rows are not an article to fetch and read.
    return [{"id": item_id("", f"{feed['name']} {last['day']}"), "source": feed["name"], "url": "", "title": title,
             "body": (body + (f" {note}" if note else ""))[:MAX_BODY], "published_at": last["at"]}]


def _totals(hist: list, unit: str) -> tuple[str, str]:
    """For an amount that adds up (rainfall): totals to the newest day, and how its 30-day total compares with the same
    30 days in earlier years on record. Returns (what to add to the title, the body)."""
    import statistics
    from datetime import date, timedelta
    by_day = {r["day"]: r["value"] for r in hist}
    end = date.fromisoformat(hist[-1]["day"])
    def total(to: date, days: int) -> tuple[float, int]:
        got = [by_day[d] for d in ((to - timedelta(days=i)).isoformat() for i in range(days)) if d in by_day]
        return round(sum(got), 4), len(got)
    week, month = total(end, 7)[0], total(end, 30)[0]
    so_far = total(end, end.day)[0]
    title = f"; {week:g} {unit} in 7 days, {month:g} in 30 days, {so_far:g} this month so far"
    body = "Measured daily total, not news."
    past = []
    for back in range(1, 60):
        try:
            then = end.replace(year=end.year - back)
        except ValueError:                      # 29 February
            then = end.replace(year=end.year - back, day=28)
        t, n = total(then, 30)
        if n >= 27:
            past.append(t)
    if len(past) >= 5:
        below = sum(p < month for p in past)
        body += (f" The 30-day total of {month:g} {unit} is higher than in {below} of the {len(past)} earlier years on record for the same 30 days"
                 f" (those ranged from {min(past):g} to {max(past):g}, median {statistics.median(past):g}).")
    return title, body


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
            kind = f.get("kind") or "rss"
            if kind == "rss":
                rows = parse_feed(f["name"], fetch(f["url"]))
                ok = bool(rows)                                        # an empty feed counts as a failure
            else:
                if get is None:
                    from .fetch import http_get
                    get = lambda url: http_get(url)[1]
                spec = {**notes.get(f["name"], {}), **f}
                if isinstance(spec.get("recipe"), str):
                    spec["recipe"] = json.loads(spec["recipe"] or "{}")
                if kind == "kalshi":
                    from . import forecasts
                    rows, ok = forecasts.moves(conn, now), True            # a quiet day is a success with no items
                elif kind == "page":
                    rows, ok = read_page(conn, spec, get(expand(f["url"], now)), now)   # an unchanged page is a success with no items
                else:
                    past = spec.get("history") or (spec.get("recipe") or {}).get("history")
                    done = f"history_read:{f['name']}"
                    if past and not conn.execute("SELECT 1 FROM kv WHERE k = ?", (done,)).fetchone():
                        try:                                           # the long record, read once; the daily read never waits on it
                            if read_series(conn, spec, get(expand(str(past), now)), now):
                                with conn:
                                    conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (done, str(now)))
                        except Exception as e:                         # asked for again at the next intake
                            print(f"history for {f['name']} not read: {type(e).__name__}: {str(e)[:120]}", flush=True)
                    rows = read_series(conn, spec, get(expand(f["url"], now)), now)
                    ok = bool(rows)
            report[f["name"]] = store(conn, rows, now)
            sources.record_fetch(conn, f["name"], ok, now)
        except Exception as e:
            report[f["name"]] = f"error: {str(e)[:80]}"
            sources.record_fetch(conn, f["name"], False, now)
    return report
