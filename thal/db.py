"""SQLite storage for thalamus v2.

One file holds every layer that is not the wiki:
  items/events  - the append-only log of everything ingested (the "transcript" layer)
  runs          - every reasoning run, searchable
  fuses         - open hypotheses waiting on evidence (the "notes" layer)
  bets/marks    - the ledger and its scores
  orders        - what the broker was asked to do
  bars          - cached prices
All timestamps are UTC epoch seconds.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "thal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,            -- sha256(source|url|title)[:20]
    source TEXT NOT NULL,
    url TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    published_at REAL,
    fetched_at REAL NOT NULL,
    extracted INTEGER NOT NULL DEFAULT 0   -- 0 pending, 1 done, 2 skipped as noise
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(title, body, content='items', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    at REAL NOT NULL,               -- when it happened (best estimate), else fetched time
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    entities TEXT NOT NULL DEFAULT '[]',     -- JSON list of names
    commodities TEXT NOT NULL DEFAULT '[]',
    places TEXT NOT NULL DEFAULT '[]',
    magnitude INTEGER NOT NULL DEFAULT 1,    -- 1 minor .. 5 systemic
    novelty INTEGER NOT NULL DEFAULT 1,      -- 1 rehash .. 5 first report
    reliability INTEGER NOT NULL DEFAULT 3,  -- 1 rumor .. 5 primary source
    model TEXT,
    connected INTEGER NOT NULL DEFAULT 0     -- has the connect step seen it
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(summary, entities, commodities, places, content='events', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary, entities, commodities, places)
    VALUES (new.id, new.summary, new.entities, new.commodities, new.places);
END;

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    kind TEXT NOT NULL,             -- extract | connect | thesis | dream
    model TEXT,
    input TEXT NOT NULL,
    output TEXT NOT NULL,
    in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0,
    cost REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fuses (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    title TEXT NOT NULL,
    thesis TEXT NOT NULL,
    watch TEXT NOT NULL DEFAULT '[]',        -- JSON list: evidence it is waiting for
    status TEXT NOT NULL DEFAULT 'open',     -- open | fired | dead | expired
    expires_at REAL,
    pages TEXT NOT NULL DEFAULT '[]'         -- JSON list of wiki slugs
);
CREATE TABLE IF NOT EXISTS fuse_evidence (
    id INTEGER PRIMARY KEY,
    fuse_id INTEGER NOT NULL REFERENCES fuses(id),
    event_id INTEGER REFERENCES events(id),
    at REAL NOT NULL,
    direction TEXT NOT NULL,                 -- supports | contradicts
    note TEXT NOT NULL,
    UNIQUE(fuse_id, event_id)
);

CREATE TABLE IF NOT EXISTS page_notes (      -- inbox: notes waiting to be folded into a wiki page by the dream step
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL,
    events TEXT NOT NULL DEFAULT '[]',
    folded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,                -- when the idea was issued
    source TEXT NOT NULL,                    -- v1 | v2
    kind TEXT NOT NULL,                      -- alert | revise | kill | paper | live
    fuse_id INTEGER REFERENCES fuses(id),
    symbol TEXT NOT NULL,
    hedge_symbol TEXT,                       -- short leg of a spread, if any
    asset_class TEXT NOT NULL DEFAULT 'equity',
    side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
    thesis TEXT NOT NULL DEFAULT '',
    invalidation TEXT NOT NULL DEFAULT '',
    horizon_days INTEGER,
    benchmark TEXT,                          -- sector benchmark, besides SPY
    tags TEXT NOT NULL DEFAULT '[]',
    entry_at REAL, entry_px REAL
);
CREATE TABLE IF NOT EXISTS marks (
    bet_id INTEGER NOT NULL REFERENCES bets(id),
    horizon TEXT NOT NULL,                   -- 1w | 4w | 12w | now
    ret REAL NOT NULL,                       -- signed by side
    vs_spy REAL, vs_bench REAL,
    marked_at REAL NOT NULL,
    PRIMARY KEY (bet_id, horizon)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    bet_id INTEGER REFERENCES bets(id),
    mode TEXT NOT NULL,                      -- paper | live
    symbol TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('BUY','SELL')),
    qty REAL NOT NULL,
    status TEXT NOT NULL,                    -- pending | filled | rejected | cancelled
    reason TEXT NOT NULL DEFAULT '',
    fill_at REAL, fill_px REAL
);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, ts REAL NOT NULL, close REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS bars_meta (symbol TEXT PRIMARY KEY, fetched_at REAL NOT NULL, ok INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',   -- trial | active | retired
    added_at REAL NOT NULL,
    added_by TEXT NOT NULL DEFAULT 'seed',   -- seed | scout
    why TEXT NOT NULL DEFAULT '',
    fails INTEGER NOT NULL DEFAULT 0,        -- consecutive fetches that returned nothing
    last_ok_at REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    from .paths import db_path
    path = db_path() if path is None else path
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for col in ("fulltext TEXT", "fetch_status TEXT"):          # added after the first databases were created
        if col.split()[0] not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    for col in ("dup_of INTEGER", "sources INTEGER NOT NULL DEFAULT 1"):   # the same story from several feeds is merged into one
        if col.split()[0] not in have:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col}")
    return conn


def now() -> float:
    return time.time()


def jl(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query (FTS5 syntax errors on raw punctuation)."""
    words = ["".join(ch for ch in w if ch.isalnum()) for w in text.split()]
    words = [w for w in words if len(w) > 2]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
