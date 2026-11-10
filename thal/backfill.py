"""Load the old bot's saved headline scans into the event log as history.

The old bot saved every scan as a JSON file: {"timestamp": ..., "headlines": [{source, title, link, summary}]}.
Loading them gives memory months of dated events to connect against. Backfilled items are never
fetched (old links, and no need), are extracted by this module only, and their events are marked as
already seen so the analyst is not handed months of old news as if it were new. Their source is
prefixed "v1: " so the yield statistics of live sources are not distorted.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
import time
from pathlib import Path

from . import extract
from .ingest import MAX_BODY, clean, item_id
from .llm import LLM

PENDING = 3          # items.extracted value for "backfilled, waiting for extraction"; the live intake ignores it
PREFIX = "v1: "


def scan_time(path: Path, data: dict) -> float | None:
    """When the scan ran, from the file name (YYYYMMDD_HHMMSS, UTC) or its timestamp field."""
    for text in (path.stem[:15], str(data.get("timestamp", ""))[:15]):
        try:
            return float(calendar.timegm(time.strptime(text, "%Y%m%d_%H%M%S")))
        except ValueError:
            continue
    return None


def load(conn: sqlite3.Connection, folder: str | Path) -> dict:
    """Insert every headline not already known, dated by the first scan that saw it. Safe to run twice."""
    rep = {"files": 0, "skipped_files": 0, "headlines": 0, "new": 0}
    for path in sorted(Path(folder).glob("*.json")):
        try:
            data = json.loads(path.read_text())
            heads, ts = data["headlines"], scan_time(path, data)
            if ts is None or not isinstance(heads, list):
                raise ValueError("not a scan file")
        except Exception:
            rep["skipped_files"] += 1
            continue
        rep["files"] += 1
        with conn:
            for h in heads:
                title = clean(h.get("title")) if isinstance(h, dict) else ""
                if not title:
                    continue
                url = str(h.get("link") or "").strip()
                rep["headlines"] += 1
                rep["new"] += conn.execute(
                    "INSERT OR IGNORE INTO items(id, source, url, title, body, published_at, fetched_at, extracted, fetch_status)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (item_id(url, title), PREFIX + clean(str(h.get("source") or "unknown"))[:60], url, title,
                     clean(h.get("summary"))[:MAX_BODY], ts, ts, PENDING, "backfill")).rowcount
    return rep


def extract_backfill(conn: sqlite3.Connection, llm: LLM, limit: int | None = None, shard: tuple[int, int] = (0, 1),
                     now: float | None = None, progress=None) -> dict:
    """Extract events from backfilled items, oldest first. A failed batch stays pending; stops when the budget does.
    shard=(i, n) takes every n-th item, so several processes can share the work."""
    rows = conn.execute("SELECT * FROM items WHERE extracted = ? AND rowid % ? = ? ORDER BY fetched_at, rowid"
                        + (f" LIMIT {int(limit)}" if limit else ""), (PENDING, shard[1], shard[0])).fetchall()
    done = {"items": 0, "events": 0, "failed_batches": 0}
    for i in range(0, len(rows), extract.BATCH):
        batch = rows[i:i + extract.BATCH]
        try:
            done["events"] += extract.extract_batch(conn, llm, batch, now, connected=1)
            done["items"] += len(batch)
        except Exception as e:
            done["failed_batches"] += 1
            done["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            if type(e).__name__ == "BudgetExceeded":
                break
        if progress and (i // extract.BATCH) % 10 == 0:
            progress(done, len(rows))
    return done


def pending(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM items WHERE extracted = ?", (PENDING,)).fetchone()[0]
