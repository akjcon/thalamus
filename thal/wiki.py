"""The wiki: linked markdown pages the model maintains (the "representations" layer).

Pages are plain files so they can be read and diffed by a person. Writes are guarded:
a writer must pass the revision it read, so two writers cannot silently overwrite each
other (that failure lost 28% of facts in Anthropic's shared-table experiment, and it
is the same class of bug as v1's dropped world-model updates).
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "wiki"
LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
KINDS = ("commodity", "chokepoint", "company", "country", "theme", "rule", "legacy")
MAX_PAGE_BYTES = 12_000     # a page that outgrows this should be split, not extended


class StaleWrite(Exception):
    """The page changed since the writer read it."""


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        raise ValueError(f"cannot make a page name from {name!r}")
    return s[:80]


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    kind: str
    updated: str
    body: str
    rev: str

    @property
    def links(self) -> list[str]:
        return list(dict.fromkeys(slugify(m) for m in LINK.findall(self.body)))


def _rev(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class Wiki:
    def __init__(self, root: str | Path = DEFAULT_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, slug: str) -> Path:
        slug = slugify(slug)                       # also stops ../ escapes
        return self.root / f"{slug}.md"

    def slugs(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.md"))

    def read(self, slug: str) -> Page | None:
        p = self._path(slug)
        if not p.exists():
            return None
        text = p.read_text()
        meta, body = {}, text
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            head, body = text[4:].split("\n---\n", 1)
            for line in head.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
        return Page(p.stem, meta.get("title", p.stem), meta.get("kind", "theme"), meta.get("updated", ""),
                    body.strip("\n"), _rev(text))

    def write(self, slug: str, title: str, kind: str, body: str, expect_rev: str | None = None,
              today: str | None = None) -> Page:
        """Create or replace a page. expect_rev must be the rev that was read (None only for a new page)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        body = body.strip("\n")
        if not body.strip():
            raise ValueError("refusing to write an empty page")
        if len(body.encode()) > MAX_PAGE_BYTES:
            raise ValueError(f"page is {len(body.encode())} bytes; split it (limit {MAX_PAGE_BYTES})")
        p = self._path(slug)
        current = self.read(slug)
        if current is None and expect_rev is not None:
            raise StaleWrite(f"{p.stem} no longer exists")
        if current is not None and current.rev != expect_rev:
            raise StaleWrite(f"{p.stem} changed since it was read (have {expect_rev}, now {current.rev})")
        title = " ".join(title.split()) or p.stem
        text = f"---\ntitle: {title}\nkind: {kind}\nupdated: {today or time.strftime('%Y-%m-%d')}\n---\n{body}\n"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, p)                         # atomic: a crash never leaves half a page
        return self.read(slug)

    def backlinks(self, slug: str) -> list[str]:
        slug = slugify(slug)
        return [s for s in self.slugs() if slug in self.read(s).links]

    def broken_links(self) -> dict[str, list[str]]:
        have = set(self.slugs())
        out = {}
        for s in have:
            missing = [l for l in self.read(s).links if l not in have]
            if missing:
                out[s] = missing
        return out

    def orphans(self) -> list[str]:
        linked = {l for s in self.slugs() for l in self.read(s).links}
        return [s for s in self.slugs() if s not in linked]

    def stats(self) -> dict:
        pages = [self.read(s) for s in self.slugs()]
        return {"pages": len(pages), "links": sum(len(p.links) for p in pages),
                "bytes": sum(len(p.body.encode()) for p in pages)}

    def search(self, query: str, limit: int = 5) -> list[tuple[str, float]]:
        """Rank pages by how many query words they contain; title hits count triple."""
        words = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower())}
        scored = []
        for s in self.slugs():
            p = self.read(s)
            title, body = p.title.lower() + " " + s, p.body.lower()
            score = sum(3 * (w in title) + min(body.count(w), 5) / 5 for w in words)
            if score > 0:
                scored.append((s, round(score, 3)))
        return sorted(scored, key=lambda x: (-x[1], x[0]))[:limit]

    def index(self) -> str:
        """One line per page: what the dashboard shows so the model knows what exists."""
        return "\n".join(f"- [[{s}]] ({p.kind}, {p.updated}) {p.title}" for s in self.slugs() if (p := self.read(s)))
