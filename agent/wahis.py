"""
Thalamus WAHIS fetcher — global animal-disease events from WOAH's WAHIS
(wahis.woah.org).

WAHIS is the authoritative global notifiable-disease notification system: every
member country must report new outbreaks ("immediate notifications") within 24h,
plus periodic "follow-up reports" on ongoing events. This is the leading-indicator
net for biological supply shocks (HPAI, ASF, FMD, screwworm, PPR…) — often days
ahead of the trade press, and global rather than US/Mexico-centric like our feeds.

There is no RSS. The public site is a Cloudflare-protected Angular SPA whose data
comes from POST /api/v1/pi/event/filtered-list. Plain HTTP (even with browser
headers / TLS impersonation) gets a Cloudflare 403; the old /pi/ path the public
scrapers used is dead. We drive a stealth headless browser to clear Cloudflare's
managed challenge, then issue the exact POST the SPA does and parse the event list.

Returns headline dicts shaped like scanner.pull_feeds() entries, so events flow
through the normal classify → analyze pipeline. ANY failure returns [] (never
raises): if Cloudflare blocks Railway's datacenter IP, the cycle degrades
gracefully to RSS-only.
"""

import json
from datetime import datetime, timezone, timedelta

WAHIS_URL = "https://wahis.woah.org/"
API_PATH = "/api/v1/pi/event/filtered-list?language=en"
EVENT_URL = "https://wahis.woah.org/#/event-management?reportId={report_id}"
# Linux UA — matches Railway's runtime; bundled Chromium confirmed to clear CF.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Cloudflare managed-challenge timing. Longer first wait for the initial challenge
# JS to run; shorter on retries (the challenge is usually already in flight).
# FETCH_TIMEOUT_MS bounds a hung in-page fetch so it can't freeze the scan loop.
CF_WAIT_FIRST_MS = 5000
CF_WAIT_RETRY_MS = 4000
FETCH_TIMEOUT_MS = 20000
NAV_TIMEOUT_MS = 60000


def _build_filter(report_types: list[str], page_size: int) -> dict:
    """The exact filter body the SPA POSTs to /api/v1/pi/event/filtered-list.
    All keys are required even when empty — the API expects the full shape, so
    do NOT prune the empty lists. Sorted newest-submission-first."""
    return {
        "animalTypes": [], "countries": [], "eventIds": [], "eventStartDate": None,
        "eventStatuses": [], "firstDiseases": [], "reasons": [], "reportIds": [],
        "reportStatuses": [], "reportTypes": report_types, "secondDiseases": [],
        "sortColumn": "submissionDate", "sortOrder": "desc", "submissionDate": None,
        "typeStatuses": [], "pageNumber": 0, "pageSize": page_size,
    }


def _clean_disease(d: str) -> str:
    """Trim WAHIS's verbose disease strings, e.g.
    'Influenza A viruses of high pathogenicity (Inf. with) (non-poultry...)'."""
    return " ".join((d or "").replace("(Inf. with)", "").split()).strip()


def _fetch_raw(report_types: list[str], page_size: int, retries: int = 3) -> list[dict]:
    """Drive a stealth headless browser to clear Cloudflare and POST the filter.
    Returns the raw event list, or [] on any failure / persistent CF block."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    body = _build_filter(report_types, page_size)
    launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]

    # Stealth().use_sync wraps playwright so every context/page auto-applies the
    # evasion patches (navigator.webdriver, chrome.runtime, etc.) — same pattern
    # brokerage.py uses against Schwab's bot detection.
    with Stealth().use_sync(sync_playwright()) as p:
        browser = None
        try:
            # Prefer real Chrome — Chromium's TLS fingerprint is more detectable
            # even with JS evasion. Railway has no real Chrome, so it falls back to
            # bundled Chromium (confirmed to clear CF). Log which one, since that's
            # the key diagnostic if the datacenter IP gets challenged.
            try:
                browser = p.chromium.launch(channel="chrome", headless=True, args=launch_args)
                print("  WAHIS: using installed Chrome")
            except Exception:
                browser = p.chromium.launch(headless=True, args=launch_args)
                print("  WAHIS: using bundled Chromium")

            ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900},
                                      locale="en-US")
            page = ctx.new_page()
            page.goto(WAHIS_URL, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            for attempt in range(retries):
                # Give Cloudflare's managed challenge time to clear before calling.
                page.wait_for_timeout(CF_WAIT_FIRST_MS if attempt == 0 else CF_WAIT_RETRY_MS)
                # POST from inside the cleared page context. AbortController bounds a
                # hung fetch so a stalled request can't block the executor thread.
                res = page.evaluate(
                    """async ({path, body, timeoutMs}) => {
                        const ctrl = new AbortController();
                        const tid = setTimeout(() => ctrl.abort(), timeoutMs);
                        try {
                            const r = await fetch(path, {
                                method: 'POST',
                                headers: {'Content-Type':'application/json','Accept':'application/json'},
                                body: JSON.stringify(body),
                                signal: ctrl.signal
                            });
                            return {status: r.status, body: await r.text()};
                        } catch (e) {
                            return {status: -1, body: String(e)};
                        } finally {
                            clearTimeout(tid);
                        }
                    }""",
                    {"path": API_PATH, "body": body, "timeoutMs": FETCH_TIMEOUT_MS},
                )
                if res["status"] == 200:
                    try:
                        # API wraps the events as {"list": [...]}.
                        return json.loads(res["body"]).get("list", []) or []
                    except Exception as e:
                        print(f"  [!] WAHIS: 200 but unparseable JSON: {e}")
                        return []
                print(f"  [wahis] attempt {attempt+1}/{retries}: status {res['status']} "
                      f"(Cloudflare not cleared yet)")
            print("  [!] WAHIS: gave up after retries — likely Cloudflare block (datacenter IP?)")
            return []
        finally:
            if browser is not None:
                browser.close()


def fetch_wahis_events(config: dict) -> list[dict]:
    """Fetch recent WAHIS events as scanner-compatible headline dicts.
    Collapses multiple reports of the same event to its latest state, applies a
    recency window, and sorts new immediate-notifications first. Returns [] if
    disabled or on any failure."""
    cfg = (config or {}).get("wahis", {})
    if not cfg.get("enabled"):
        return []

    report_types = cfg.get("report_types", ["IN", "FUR"])
    max_events = cfg.get("max_events", 25)
    recency_days = cfg.get("recency_days", 21)
    page_size = cfg.get("page_size", 120)

    try:
        raw = _fetch_raw(report_types, page_size)
    except Exception as e:
        print(f"  [!] WAHIS fetch crashed (continuing without it): {e}")
        return []
    if not raw:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)

    # Recency filter + collapse by eventId (keep the latest submission per event,
    # so 10 routine follow-ups on one outbreak become a single current-state item).
    # Raw event keys used: eventId, country, disease, reason, eventStatus,
    # reportType, reportNumber, reportId, submissionDate. A bad record is skipped,
    # not allowed to abort the whole batch.
    by_event: dict = {}
    for e in raw:
        try:
            sub = e.get("submissionDate") or ""
            sdt = datetime.fromisoformat(sub.replace("Z", "+00:00"))
            if sdt.tzinfo is None:  # date-only values parse naive — make tz-aware
                sdt = sdt.replace(tzinfo=timezone.utc)
            if sdt < cutoff:
                continue
            # Fall back to reportId when eventId is missing, so distinct events
            # don't collapse under a shared None key.
            eid = e.get("eventId") or f"_rid:{e.get('reportId')}"
            prev = by_event.get(eid)
            if prev is None or sdt > prev[0]:
                by_event[eid] = (sdt, e)
        except Exception:
            continue

    # New immediate notifications first (reportType != "IN" → False sorts first),
    # then newest submission; reportId as a stable tie-breaker so the max_events
    # truncation is deterministic across cycles.
    collapsed = sorted(
        by_event.values(),
        key=lambda t: (t[1].get("reportType") != "IN", -t[0].timestamp(), t[1].get("reportId") or 0),
    )

    headlines = []
    in_count = 0
    for sdt, e in collapsed[:max_events]:
        try:
            rid = e.get("reportId")
            if rid is None:
                continue  # no stable identity → can't dedup/link reliably, skip
            country = e.get("country", "?")
            disease = _clean_disease(e.get("disease"))
            reason = (e.get("reason") or "").strip()
            status = e.get("eventStatus", "")
            rtype = e.get("reportType", "")
            rnum = e.get("reportNumber")
            is_in = rtype == "IN"
            in_count += int(is_in)
            kind = "new immediate notification" if is_in else f"follow-up report #{rnum}"

            title = f"{country} — {disease} [{status}]"
            if reason:
                title += f" — {reason}"
            summary = (f"WOAH/WAHIS {kind}. Disease: {disease}. Country: {country}. "
                       f"Event status: {status}. Reason: {reason or 'n/a'}. "
                       f"Submitted {sdt.strftime('%Y-%m-%d')}.")
            headlines.append({
                "source": "WAHIS (WOAH)",
                "title": title,
                "link": EVENT_URL.format(report_id=rid),
                "summary": summary,
            })
        except Exception:
            continue

    print(f"  WAHIS: {len(headlines)} events ({in_count} new IN, "
          f"{len(headlines)-in_count} follow-ups) from {len(raw)} reports")
    return headlines


if __name__ == "__main__":
    import yaml
    from pathlib import Path
    ROOT = Path(__file__).parent.parent
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
    cfg.setdefault("wahis", {})["enabled"] = True  # force-enable for standalone test
    evs = fetch_wahis_events(cfg)
    print(f"\n{len(evs)} headlines:")
    for h in evs:
        print(f"  [{h['source']}] {h['title']}")
        print(f"        {h['summary']}")
