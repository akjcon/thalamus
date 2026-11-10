"""Get the full text behind a link, not just the feed's headline.

Plain HTTP plus readable-text extraction handles most pages cheaply. The client says
what it is, and a site's refusal (401, 403, 429, a bot wall, a paywall) is final: such
pages are recorded as unreadable and never retried by other means. A headless browser
can optionally render pages that simply need JavaScript; it is off unless
THAL_BROWSER=1, and it is never used on a page that refused the plain request.
Every URL, including each redirect hop, must be a public http(s) address.
"""
from __future__ import annotations

import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

import html as _html
import os
import re

from .sources import safe_url

DECLARED_SITES = ("sec.gov",)      # sites whose policy requires automated clients to identify themselves with a contact
EDGAR_DOC = re.compile(r'href="(?:/ix\?doc=)?(/Archives/edgar/data/\d+/\d+/[^"]+?\.htm)"', re.I)

UA = "Mozilla/5.0 (compatible; thalamus-research/0.1; automated feed and article reader)"
MAX_BYTES = 3_000_000
MAX_TEXT = 20_000
MIN_TEXT = 400          # less than this is a stub, a paywall teaser or a failed extraction
TIMEOUT = 15
HOST_DELAY = 1.0        # seconds between requests to the same site
GIVE_UP_AFTER = 6       # a source whose last pages all failed is skipped until one succeeds elsewhere
WALLS = ("undeclared automated tool", "access denied", "forbidden", "you don't have permission", "are you a robot",
         "verify you are human", "enable javascript", "captcha", "you need to be signed in", "subscribe to continue",
         "subscribe to read", "already a subscriber", "sign in to continue", "unusual traffic")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(urljoin(req.full_url, newurl))        # a redirect cannot lead somewhere private
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def client_name(url: str, env: dict | None = None) -> str:
    """A browser-like client string everywhere, except sites that require a declared client: those get a name and
    the contact address from THAL_CONTACT_EMAIL. The address goes to those sites only, and only if it is set."""
    env = os.environ if env is None else env
    host = (urlparse(url).hostname or "").lower()
    contact = (env.get("THAL_CONTACT_EMAIL") or "").strip()
    if contact and any(host == s or host.endswith("." + s) for s in DECLARED_SITES):
        return f"thalamus-research {contact}"
    return UA


def http_get(url: str) -> tuple[str, str]:
    """Return (content type, body text). Raises on any failure."""
    from .llm import load_env
    load_env()
    opener = urllib.request.build_opener(_CheckedRedirects)
    req = urllib.request.Request(safe_url(url), headers={"User-Agent": client_name(url), "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                                         "Accept-Language": "en-US,en;q=0.9"})
    with opener.open(req, timeout=TIMEOUT) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        raw = r.read(MAX_BYTES + 1)[:MAX_BYTES]
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        return ctype, raw.decode(charset, errors="replace")
    except LookupError:
        return ctype, raw.decode("utf-8", errors="replace")


def readable(html: str, url: str = "") -> str:
    """Main article text of an HTML page, without navigation and boilerplate."""
    import trafilatura
    text = trafilatura.extract(html, url=url or None, include_comments=False, include_tables=True, favor_recall=True) or ""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def wall(text: str) -> str:
    """Name the block or paywall marker near the top of the text, or '' if it looks like real content."""
    head = text[:600].lower()
    return next((w for w in WALLS if w in head), "")


def browser_get(url: str) -> str:
    """Render the page in a headless browser. Returns '' if no browser is installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    safe_url(url)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            page = b.new_context(user_agent=UA).new_page()       # the same honest client string as plain requests
            page.route("**/*", lambda route: route.continue_() if _public(route.request.url) else route.abort())
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
            page.wait_for_timeout(2500)
            return page.content()
        finally:
            b.close()


def _public(url: str) -> bool:
    try:
        safe_url(url)
        return True
    except ValueError:
        return urlparse(url).scheme in ("data", "blob", "about")


def edgar_documents(index_html: str, base: str) -> list[str]:
    """The filing's own documents linked from an EDGAR filing index page, exhibits first.

    Exhibits (the press release, the agreement) carry the substance; the form itself is mostly cover page."""
    docs = list(dict.fromkeys(urljoin(base, m) for m in EDGAR_DOC.findall(index_html) if "-index" not in m.lower()))
    is_exhibit = lambda u: bool(re.search(r"ex[-_]?\d", u.rsplit("/", 1)[-1].lower()))
    return sorted(docs, key=lambda u: not is_exhibit(u))[:4]


def plain(html: str) -> str:
    """All visible text of a page with tags removed. Only for documents with no site chrome, such as filings."""
    html = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/tr|/h\d|/li)\b[^>]*>", "\n", html)
    text = _html.unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    lines = (" ".join(l.split()) for l in text.splitlines())
    return "\n".join(l for l in lines if l)


def fetch_text(url: str, get=http_get, browser=None, pause=time.sleep) -> tuple[str, str]:
    """Return (text, how). how is 'http', 'browser', or the reason it failed."""
    why = ""
    if browser is None:
        browser = lambda u: ""
    try:
        ctype, body = get(url)
        if url.lower().endswith("-index.htm") and "/archives/edgar/" in url.lower():
            parts = []                       # an EDGAR index page only lists the filing; read the documents themselves
            for doc in edgar_documents(body, url):
                pause(0.3)
                try:
                    raw = get(doc)[1]
                    t = readable(raw, doc)
                    if len(t) < 200:             # table-heavy exhibits defeat article extraction; they have no chrome to strip
                        t = plain(raw)
                except Exception:
                    continue
                if len(t) >= 200 and not wall(t):
                    parts.append(f"[{doc.rsplit('/', 1)[-1]}]\n{t}")
            text = "\n\n".join(parts)
            if len(text) >= MIN_TEXT:
                return text[:MAX_TEXT], "http"
        if "pdf" in ctype:
            why = "pdf not supported yet"
        else:
            text = body if ctype.startswith("text/plain") else readable(body, url)
            if wall(text):
                why = f"walled ({wall(text)})"
            elif len(text) >= MIN_TEXT:
                return text[:MAX_TEXT], "http"
            else:
                why = f"only {len(text)} chars of text"
    except ValueError:
        return "", "blocked: not a public url"
    except urllib.error.HTTPError as e:
        why = f"http {e.code}"
    except Exception as e:
        why = f"{type(e).__name__}"
    if refused(why):
        return "", why                       # the site said no; do not try another way in
    try:
        html = browser(url)
        text = readable(html, url) if html else ""
        if wall(text):
            return "", f"{why}; browser walled ({wall(text)})"
        if len(text) >= MIN_TEXT:
            return text[:MAX_TEXT], "browser"
        return "", why + ("; browser got too little text" if html else "")
    except Exception as e:
        return "", f"{why}; browser {type(e).__name__}"


REFUSALS = ("http 401", "http 403", "http 407", "http 429", "http 451", "walled", "blocked")


def refused(why: str) -> bool:
    """True when the site declined to serve us. Those pages are left alone."""
    return why.startswith(REFUSALS)


def http_only(url: str) -> tuple[str, str]:
    return fetch_text(url, browser=lambda u: "")


def default_browser():
    """The optional renderer for JavaScript-only pages; None unless THAL_BROWSER=1."""
    from .llm import load_env
    load_env()
    return browser_text if os.environ.get("THAL_BROWSER") == "1" else None


def browser_text(url: str, browser=browser_get) -> tuple[str, str]:
    """Read a page with the headless browser alone."""
    try:
        text = readable(browser(url), url)
    except Exception as e:
        return "", f"browser {type(e).__name__}"
    if wall(text):
        return "", f"browser walled ({wall(text)})"
    return (text[:MAX_TEXT], "browser") if len(text) >= MIN_TEXT else ("", "browser got too little text")


def fetch_pending(conn: sqlite3.Connection, limit: int = 2000, fetcher=http_only, sleep=time.sleep, clock=time.monotonic,
                  workers: int = 12, browser="default", browser_tries: int = 3) -> dict:
    """Fetch full text for items that have a link and have not been tried.

    Sites are fetched in parallel over plain HTTP, each site one page at a time with a pause between pages.
    If the optional browser is enabled, pages that returned too little text (JavaScript-rendered) are rendered
    one at a time on the calling thread; pages the site refused are never retried. A source whose recent pages all failed gets a single probe per run, so it can recover."""
    from concurrent.futures import ThreadPoolExecutor
    rows = conn.execute("SELECT id, source, url FROM items WHERE fetch_status IS NULL AND url != '' ORDER BY fetched_at DESC, rowid"
                        " LIMIT ?", (limit,)).fetchall()
    streak: dict[str, int] = {}
    for src in {r["source"] for r in rows}:      # how many of this source's most recent attempts failed in a row
        recent = [x[0] for x in conn.execute("SELECT fetch_status FROM items WHERE source = ? AND fetch_status IS NOT NULL"
                                             " ORDER BY fetched_at DESC, rowid DESC LIMIT ?", (src, GIVE_UP_AFTER))]
        streak[src] = len(recent) if len(recent) == GIVE_UP_AFTER and all(s not in ("http", "browser") for s in recent) else 0
    by_host: dict[str, list] = {}
    for r in rows:
        by_host.setdefault(urlparse(r["url"]).hostname or "", []).append(r)

    def work(host_rows):
        out, skipped, last = [], [], None
        for r in host_rows:
            src = r["source"]
            if streak[src] >= GIVE_UP_AFTER:
                if src in skipped:
                    continue
                skipped.append(src)          # the one probe for this run
            if last is not None and (wait := HOST_DELAY - (clock() - last)) > 0:
                sleep(wait)
            try:
                text, how = fetcher(r["url"])
            except Exception as e:           # one bad page never stops the rest
                text, how = "", f"{type(e).__name__}"
            last = clock()
            out.append((r["id"], text, how))
            streak[src] = 0 if text else streak[src] + 1
            if text and src in skipped:
                skipped.remove(src)
        return out, skipped

    rep = {"tried": 0, "ok": 0, "failed": 0, "by_browser": 0, "skipped_sources": []}
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for out, skipped in pool.map(work, by_host.values()):      # results are written here, on the calling thread
            with conn:
                conn.executemany("UPDATE items SET fulltext = ?, fetch_status = ? WHERE id = ?",
                                 [(text or None, how[:120], i) for i, text, how in out])
            rep["tried"] += len(out)
            rep["ok"] += sum(1 for _, text, _ in out if text)
            failed += [(i, how) for i, text, how in out if not text and not refused(how) and not how.startswith("pdf")]
            rep["skipped_sources"] += [s for s in skipped if s not in rep["skipped_sources"]]
    if browser == "default":
        browser = default_browser()
    src_url = {r["id"]: (r["source"], r["url"]) for r in rows}
    tries: dict[str, int] = {}
    works: dict[str, bool] = {}
    for item_id, why in failed if browser else []:
        src, url = src_url[item_id]
        if works.get(src) is False:
            continue
        text, how = browser(url)
        if text:
            works[src] = True
            with conn:
                conn.execute("UPDATE items SET fulltext = ?, fetch_status = 'browser' WHERE id = ?", (text, item_id))
            rep["ok"] += 1
            rep["by_browser"] += 1
        else:
            with conn:
                conn.execute("UPDATE items SET fetch_status = ? WHERE id = ?", (f"{why}; {how}"[:120], item_id))
            tries[src] = tries.get(src, 0) + 1
            if tries[src] >= browser_tries and not works.get(src):
                works[src] = False           # the browser does not get through this site either
    rep["failed"] = rep["tried"] - rep["ok"]
    rep["skipped_sources"].sort()
    return rep


def coverage(conn: sqlite3.Connection) -> list[dict]:
    """Per source: how often the full text could be read, and the most common reason when it could not."""
    out = []
    for r in conn.execute("SELECT source, COUNT(*) n, SUM(fulltext IS NOT NULL) ok FROM items WHERE fetch_status IS NOT NULL GROUP BY 1 ORDER BY 1"):
        why = conn.execute("SELECT fetch_status, COUNT(*) c FROM items WHERE source = ? AND fulltext IS NULL AND fetch_status IS NOT NULL"
                           " GROUP BY 1 ORDER BY c DESC LIMIT 1", (r["source"],)).fetchone()
        out.append({"source": r["source"], "tried": r["n"], "ok": r["ok"], "main_failure": why["fetch_status"] if why else ""})
    return out
