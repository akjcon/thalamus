"""Fuses, the page-note inbox, and the dashboard that is injected into every reasoning run.

The dashboard is the top memory layer, so it is kept small on purpose (the article's
rule: long top layers hurt, and a wrong note there is carried into every session).
Details live in the wiki and the event log and are searched for when needed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import db, forecasts, ledger
from .wiki import KINDS, Wiki, slugify

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400.0
MAX_DASHBOARD_LINES = 200
MAX_DASHBOARD_BYTES = 25_000
MAX_OPEN_FUSES = 40


def open_fuse(conn: sqlite3.Connection, title: str, thesis: str, watch: list[str], expires_days: int = 90,
              pages: list[str] | None = None, now: float | None = None) -> int:
    now = now or time.time()
    title, thesis = " ".join(title.split()), thesis.strip()
    watch = [w.strip() for w in watch if w and w.strip()]
    if not title or not thesis:
        raise ValueError("a fuse needs a title and a thesis")
    if not watch:
        raise ValueError("a fuse must say what evidence it is waiting for")
    n = conn.execute("SELECT COUNT(*) FROM fuses WHERE status = 'open'").fetchone()[0]
    if n >= MAX_OPEN_FUSES:
        raise ValueError(f"{n} fuses are already open; close some first")
    days = max(7, min(365, int(expires_days)))
    with conn:
        cur = conn.execute(
            "INSERT INTO fuses(created_at, updated_at, title, thesis, watch, expires_at, pages) VALUES (?,?,?,?,?,?,?)",
            (now, now, title[:160], thesis[:2000], db.jl(watch[:8]), now + days * DAY,
             db.jl([slugify(p) for p in (pages or [])][:8])))
    return cur.lastrowid


def add_evidence(conn: sqlite3.Connection, fuse_id: int, event_id: int | None, direction: str, note: str,
                 now: float | None = None) -> bool:
    """Attach an event to an open fuse. Returns False if the fuse is not open or the pair already exists."""
    if direction not in ("supports", "contradicts"):
        raise ValueError(f"direction must be supports or contradicts, got {direction!r}")
    now = now or time.time()
    f = conn.execute("SELECT status FROM fuses WHERE id = ?", (fuse_id,)).fetchone()
    if f is None or f["status"] != "open":
        return False
    if event_id is not None and conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
        return False
    with conn:
        cur = conn.execute("INSERT OR IGNORE INTO fuse_evidence(fuse_id, event_id, at, direction, note) VALUES (?,?,?,?,?)",
                           (fuse_id, event_id, now, direction, note.strip()[:500]))
        if cur.rowcount:
            conn.execute("UPDATE fuses SET updated_at = ? WHERE id = ?", (now, fuse_id))
    return bool(cur.rowcount)


def close_fuse(conn: sqlite3.Connection, fuse_id: int, status: str, now: float | None = None) -> bool:
    if status not in ("fired", "dead", "expired"):
        raise ValueError(f"cannot close a fuse as {status!r}")
    with conn:
        cur = conn.execute("UPDATE fuses SET status = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                           (status, now or time.time(), fuse_id))
    return bool(cur.rowcount)


def expire_fuses(conn: sqlite3.Connection, now: float | None = None) -> int:
    now = now or time.time()
    with conn:
        cur = conn.execute("UPDATE fuses SET status = 'expired', updated_at = ? WHERE status = 'open' AND expires_at < ?",
                           (now, now))
    return cur.rowcount


def fuse_text(conn: sqlite3.Connection, fuse_id: int, evidence_limit: int = 12) -> str:
    f = conn.execute("SELECT * FROM fuses WHERE id = ?", (fuse_id,)).fetchone()
    lines = [f"Fuse #{f['id']}: {f['title']}", f"Thesis: {f['thesis']}",
             "Waiting for: " + "; ".join(json.loads(f["watch"]))]
    ev = conn.execute("SELECT fe.direction, fe.note, e.summary FROM fuse_evidence fe LEFT JOIN events e ON e.id = fe.event_id"
                      " WHERE fe.fuse_id = ? ORDER BY fe.at DESC LIMIT ?", (fuse_id, evidence_limit)).fetchall()
    for e in ev:
        lines.append(f"  [{e['direction']}] {e['note']}" + (f" (event: {e['summary'][:200]})" if e["summary"] else ""))
    return "\n".join(lines)


def queue_note(conn: sqlite3.Connection, slug: str, title: str, kind: str, note: str, events: list[int] | None = None,
               now: float | None = None) -> int:
    """Leave a note for a wiki page. Only the dream step rewrites pages, so pages have one writer."""
    if kind not in KINDS:
        kind = "theme"
    if not note.strip():
        raise ValueError("empty note")
    if slugify(slug) == "lessons":      # that page has one writer: the weekly reflection
        raise ValueError("the lessons page is rewritten from the scored record; put process notes on another page")
    with conn:
        cur = conn.execute("INSERT INTO page_notes(at, slug, title, kind, note, events) VALUES (?,?,?,?,?,?)",
                           (now or time.time(), slugify(slug), " ".join(title.split())[:120] or slug, kind,
                            note.strip()[:1500], db.jl(events or [])))
    return cur.lastrowid


def dashboard(conn: sqlite3.Connection, wiki: Wiki, now: float | None = None) -> str:
    """The small block of context every reasoning run starts with."""
    now = now or time.time()
    rules = (ROOT / "config" / "house_rules.md").read_text().strip()
    parts = [rules, "", "## Track record (unique ideas, return vs S&P 500)"]
    board = [r for r in ledger.scoreboard(conn, by="source") if r["horizon"] in ("4w", "12w")]
    parts += [f"- {r['group']} at {r['horizon']}: n={r['n']}, mean {r['mean'] * 100:+.1f}%, "
              + (f"vs S&P {r['vs_spy'] * 100:+.1f}%" if r["vs_spy"] is not None else "no benchmark")
              for r in board] or ["- no scored bets yet"]
    parts += ["", "## Forecast record (Brier score vs the no-view baseline; lower is better)", forecasts.scores_text(conn)]
    lesson = wiki.read("lessons")
    if lesson:
        parts += ["", "## Lessons (maintained by the dream step)", lesson.body[:1500]]
    fuses = conn.execute("SELECT * FROM fuses WHERE status = 'open' ORDER BY updated_at DESC").fetchall()
    parts += ["", f"## Open fuses ({len(fuses)})"]
    for f in fuses:
        n = conn.execute("SELECT COUNT(*) FROM fuse_evidence WHERE fuse_id = ?", (f["id"],)).fetchone()[0]
        days = int((f["expires_at"] - now) / DAY) if f["expires_at"] else 0
        parts.append(f"- #{f['id']} {f['title']} [{n} evidence, {days}d left] waiting for: "
                     + "; ".join(json.loads(f["watch"]))[:240])
    if not fuses:
        parts.append("- none yet")
    parts += ["", "## Wiki pages", wiki.index() or "- none yet"]
    lines = "\n".join(parts).splitlines()
    if len(lines) > MAX_DASHBOARD_LINES:
        lines = lines[:MAX_DASHBOARD_LINES - 1] + [f"... ({len(lines) - MAX_DASHBOARD_LINES + 1} more lines cut; search the wiki)"]
    text = "\n".join(lines)
    while len(text.encode()) > MAX_DASHBOARD_BYTES:
        lines = lines[:-2] + ["... (cut to fit; search the wiki)"]
        text = "\n".join(lines)
    return text
