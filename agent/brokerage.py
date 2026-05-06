"""
Thalamus Brokerage — Read-only Schwab/thinkorswim integration.
Syncs account positions to portfolio.md. Auto-refreshes tokens
using Playwright so you never have to manually re-authenticate.

Setup:
    1. Register at https://developer.schwab.com/register
    2. Create an app with callback URL: https://127.0.0.1:8182
    3. Wait for approval (1-3 business days)
    4. Add to .env:
        SCHWAB_APP_KEY=your_app_key
        SCHWAB_APP_SECRET=your_app_secret
        SCHWAB_USERNAME=your_schwab_login
        SCHWAB_PASSWORD=your_schwab_password
    5. Run: python3 brokerage.py --login   (first-time auth, saves token)
    6. After that, token auto-refreshes — no manual intervention needed.
"""

import os
import sys
import json
import time
import base64
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT = Path(__file__).parent.parent
MEMORY = ROOT / "memory"
PORTFOLIO_FILE = MEMORY / "portfolio.md"
TOKEN_PATH = Path(os.environ.get("SCHWAB_TOKEN_PATH", str(MEMORY / ".schwab_token.json")))
STORAGE_STATE_PATH = Path(os.environ.get("SCHWAB_STORAGE_STATE_PATH", str(MEMORY / ".schwab_storage_state.json")))

SCHWAB_AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")


# ── Token management ─────────────────────────────────────────────────

def _load_token() -> dict | None:
    if TOKEN_PATH.exists():
        return json.loads(TOKEN_PATH.read_text())
    return None


def _save_token(token: dict):
    token["saved_at"] = datetime.now(timezone.utc).isoformat()
    TOKEN_PATH.write_text(json.dumps(token, indent=2))


def _token_needs_refresh(token: dict) -> bool:
    """Check if the refresh token is close to expiring (>6 days old)."""
    saved = token.get("saved_at", "")
    if not saved:
        return True
    try:
        saved_dt = datetime.fromisoformat(saved)
        age_days = (datetime.now(timezone.utc) - saved_dt).total_seconds() / 86400
        return age_days > 6  # Refresh token lasts 7 days, refresh at 6
    except Exception:
        return True


def _exchange_code_for_token(auth_code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    import httpx

    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")

    credentials = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()

    resp = httpx.post(
        SCHWAB_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": CALLBACK_URL,
        },
    )
    resp.raise_for_status()
    token = resp.json()
    _save_token(token)
    return token


def _refresh_access_token(token: dict) -> dict:
    """Use the refresh token to get a new access token."""
    import httpx

    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")

    credentials = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()

    resp = httpx.post(
        SCHWAB_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        },
    )
    resp.raise_for_status()
    new_token = resp.json()
    _save_token(new_token)
    return new_token


def _dump_debug(page, label: str) -> tuple[Path, Path]:
    """Save screenshot + HTML of the current page for debugging. Returns (png, html) paths."""
    debug_dir = MEMORY / "schwab_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    png_path = debug_dir / f"{ts}_{label}.png"
    html_path = debug_dir / f"{ts}_{label}.html"
    try:
        page.screenshot(path=str(png_path), full_page=True)
    except Exception as e:
        print(f"  [!] Screenshot failed: {e}")
    try:
        html_path.write_text(page.content())
    except Exception as e:
        print(f"  [!] HTML dump failed: {e}")
    return png_path, html_path


def _fill_first_match(page, selectors: list[str], value: str, field_label: str) -> str:
    """Try a list of selectors, type into the first one that matches.
    Uses press_sequentially (not fill) so Angular forms with autocomplete=off
    pick up the per-keystroke input/keyup events. Blurs after typing so
    client-side validation fires before submit."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=2000):
                loc.click()
                loc.press_sequentially(value, delay=30)
                loc.evaluate("el => el.blur()")
                return sel
        except Exception:
            continue
    raise RuntimeError(f"No matching selector for {field_label}. Tried: {selectors}")


def _click_first_match(page, selectors: list[str], button_label: str) -> str:
    """Try a list of selectors, click the first one that matches."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=2000):
                loc.click()
                return sel
        except Exception:
            continue
    raise RuntimeError(f"No matching selector for {button_label}. Tried: {selectors}")


def _auto_login_playwright() -> str:
    """
    Use Playwright to automate the Schwab OAuth login flow.
    Returns the authorization code from the redirect URL.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    app_key = os.environ.get("SCHWAB_APP_KEY")
    username = os.environ.get("SCHWAB_USERNAME")
    password = os.environ.get("SCHWAB_PASSWORD")
    headless = os.environ.get("SCHWAB_HEADLESS", "true").lower() not in ("false", "0", "no")

    if not username or not password:
        raise ValueError("SCHWAB_USERNAME and SCHWAB_PASSWORD must be set in .env for auto-login")

    auth_url = f"{SCHWAB_AUTH_URL}?client_id={app_key}&redirect_uri={CALLBACK_URL}"

    # Try a list of known ID patterns plus generic fallbacks
    username_selectors = [
        "#loginIdInput", "#username", "input[name='loginId']",
        "input[name='username']", "input[type='text']",
    ]
    password_selectors = [
        "#passwordInput", "#password", "input[name='password']",
        "input[type='password']",
    ]
    login_button_selectors = [
        "#btnLogin", "button[type='submit']", "input[type='submit']",
        "button:has-text('Log In')", "button:has-text('Login')", "button:has-text('Sign In')",
    ]

    # Stealth().use_sync wraps the playwright object so any context/page created
    # auto-applies the evasion patches (navigator.webdriver, chrome.runtime, etc.)
    with Stealth().use_sync(sync_playwright()) as p:
        # Prefer real installed Chrome (channel="chrome") over Chromium —
        # Akamai TLS-fingerprints Chromium binaries even with JS evasion.
        # Falls back to Chromium if Chrome isn't installed.
        try:
            browser = p.chromium.launch(channel="chrome", headless=headless)
            print("  Using installed Chrome (channel=chrome)")
        except Exception:
            browser = p.chromium.launch(headless=headless)
            print("  Falling back to bundled Chromium")
        # Pose as a real desktop Chrome so Schwab's bot detection doesn't refuse to render the
        # Angular form for a fresh datacenter headless browser.
        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        # If we have a saved trust-this-device session, load it so Schwab skips 2FA.
        if STORAGE_STATE_PATH.exists():
            ctx_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
            print(f"  Loading saved Schwab session from {STORAGE_STATE_PATH.name}")
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        try:
            print(f"  Auto-login: navigating to Schwab (headless={headless})...")
            page.goto(auth_url)

            # Wait for the actual login form to render (Angular SPA — networkidle is unreliable
            # because Angular keeps polling). Use a generous timeout to cover slow first paint.
            print("  Auto-login: waiting for login form...")
            try:
                page.wait_for_selector(
                    ", ".join(username_selectors), state="visible", timeout=45000
                )
            except Exception as wait_err:
                png, html = _dump_debug(page, "form_never_rendered")
                raise RuntimeError(
                    f"Login form never appeared. URL: {page.url}. "
                    f"Screenshot: {png}, HTML: {html}. Likely bot detection or geo-block. "
                    f"Error: {wait_err}"
                )

            print("  Auto-login: entering credentials...")
            try:
                used = _fill_first_match(page, username_selectors, username, "username")
                print(f"    matched username field: {used}")
                used = _fill_first_match(page, password_selectors, password, "password")
                print(f"    matched password field: {used}")
                # Brief settle so Angular client-side validation enables the submit button
                time.sleep(0.5)
                used = _click_first_match(page, login_button_selectors, "login button")
                print(f"    matched login button: {used}")
            except Exception as fill_err:
                png, html = _dump_debug(page, "login_form_not_found")
                raise RuntimeError(
                    f"Login form selectors did not match. URL: {page.url}. "
                    f"Screenshot: {png}, HTML: {html}. Error: {fill_err}"
                )

            # Wait for redirect to callback URL. Two scenarios:
            #  - Headless + valid trust-device cookies → instant redirect (a few seconds)
            #  - Headed + cookies expired → 2FA page; user completes it manually,
            #    checks "Trust this device", clicks submit. Wait up to 5 minutes.
            max_wait = 60 if headless else 300
            print(f"  Waiting up to {max_wait}s for redirect to callback URL...")
            if not headless:
                print("  >>> If you see a 2FA prompt, complete it in the browser window")
                print("  >>> and check 'Trust this device' before submitting.")

            redirect_url = None
            for _ in range(max_wait):
                if CALLBACK_URL.split("//")[1] in page.url:
                    redirect_url = page.url
                    break
                # Also handle any consent/accept buttons that appear
                try:
                    for sel in ["text=Accept", "text=Allow", "text=Authorize", "#acceptTerms"]:
                        btn = page.locator(sel).first
                        if btn.count() > 0 and btn.is_visible():
                            print(f"  Auto-login: clicking {sel}")
                            btn.click()
                            break
                except Exception:
                    pass
                time.sleep(1)

            if not redirect_url:
                png, html = _dump_debug(page, "no_redirect")
                hint = (
                    "Trust-device cookie likely expired. Re-run in headed mode: "
                    "SCHWAB_HEADLESS=false python3 agent/brokerage.py --login"
                ) if headless else (
                    "Did you complete 2FA and check 'Trust this device'?"
                )
                raise RuntimeError(
                    f"Auto-login did not redirect to callback URL within {max_wait}s. "
                    f"Current URL: {page.url}. Screenshot: {png}, HTML: {html}. {hint}"
                )

            # Save the trust-device cookies for next time (skips 2FA for ~30 days)
            try:
                context.storage_state(path=str(STORAGE_STATE_PATH))
                print(f"  Saved Schwab session cookies to {STORAGE_STATE_PATH.name}")
            except Exception as e:
                print(f"  [!] Could not save storage_state: {e}")
        except Exception:
            # Capture state on any unexpected failure
            try:
                _dump_debug(page, "unexpected_failure")
            except Exception:
                pass
            raise
        finally:
            browser.close()

    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]

    if not code:
        raise RuntimeError(f"No auth code in redirect URL: {redirect_url}")

    print("  Auto-login: got authorization code")
    return code


def _manual_login() -> str:
    """Open headed Chrome to Schwab OAuth URL. User logs in manually
    (handles 2FA themselves). Returns the auth code from the redirect URL."""
    from playwright.sync_api import sync_playwright

    app_key = os.environ.get("SCHWAB_APP_KEY")
    if not app_key:
        raise ValueError("SCHWAB_APP_KEY not set in .env")

    auth_url = f"{SCHWAB_AUTH_URL}?client_id={app_key}&redirect_uri={CALLBACK_URL}"

    print("\n" + "=" * 70)
    print("Schwab login required.")
    print("A browser window will open. Log in manually (handle 2FA), then")
    print("approve the OAuth consent. Script auto-detects the redirect.")
    print("=" * 70 + "\n")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        page.goto(auth_url)
        print(f"Waiting up to 5 minutes for redirect to {CALLBACK_URL}...\n")

        max_wait = 300
        for _ in range(max_wait):
            # Check the URL is actually OUR callback (post-redirect), not the OAuth
            # URL with redirectUri=... in its query params. Match by startswith.
            if page.url.startswith(CALLBACK_URL):
                redirect_url = page.url
                browser.close()
                parsed = urlparse(redirect_url)
                params = parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if not code:
                    raise RuntimeError(f"No auth code in callback URL: {redirect_url}")
                print("Login successful, got authorization code")
                return code
            time.sleep(1)

        browser.close()
        raise RuntimeError(f"Login not completed within {max_wait}s")


def ensure_valid_token() -> dict:
    """
    Get a valid token, refreshing or re-authenticating as needed.
    Refresh tokens last 7 days; access tokens 30 min (auto-refreshed each call).
    """
    token = _load_token()

    if token:
        # If refresh token is close to expiry, force a fresh manual login
        if _token_needs_refresh(token):
            print("  Refresh token expiring soon — manual re-login required")
            code = _manual_login()
            return _exchange_code_for_token(code)
        # Otherwise refresh the access token (30-min lifespan)
        try:
            return _refresh_access_token(token)
        except Exception as e:
            print(f"  Access token refresh failed: {e} — falling back to manual login")
            code = _manual_login()
            return _exchange_code_for_token(code)

    # No token at all — manual login
    code = _manual_login()
    return _exchange_code_for_token(code)


# ── API calls ─────────────────────────────────────────────────────────

def _api_get(endpoint: str) -> dict:
    """Make an authenticated GET request to the Schwab API."""
    import httpx

    token = ensure_valid_token()
    resp = httpx.get(
        f"https://api.schwabapi.com{endpoint}",
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    resp.raise_for_status()
    return resp.json()


def get_account_hashes() -> list[dict]:
    """Get the mapping of account numbers to hashes."""
    return _api_get("/trader/v1/accounts/accountNumbers")


def get_positions() -> list[dict]:
    """
    Fetch current positions from all Schwab accounts.
    Returns a list of position dicts with normalized fields.
    """
    accounts = get_account_hashes()

    all_positions = []
    for acct in accounts:
        account_hash = acct["hashValue"]
        account_num = acct.get("accountNumber", "???")

        data = _api_get(f"/trader/v1/accounts/{account_hash}?fields=positions")

        positions = data.get("securitiesAccount", {}).get("positions", [])
        balance = data.get("securitiesAccount", {}).get("currentBalances", {})

        account_info = {
            "account": account_num[-4:],  # Last 4 digits only
            "type": data.get("securitiesAccount", {}).get("type", "UNKNOWN"),
            "net_liq": balance.get("liquidationValue", 0),
            "cash": balance.get("cashBalance", 0),
            "buying_power": balance.get("buyingPower", 0),
        }

        for pos in positions:
            instrument = pos.get("instrument", {})
            all_positions.append({
                "account": account_info["account"],
                "symbol": instrument.get("symbol", "???"),
                "asset_type": instrument.get("assetType", "UNKNOWN"),
                "description": instrument.get("description", ""),
                "quantity": pos.get("longQuantity", 0) - pos.get("shortQuantity", 0),
                "avg_price": pos.get("averagePrice", 0),
                "market_value": pos.get("marketValue", 0),
                "day_pnl": pos.get("currentDayProfitLoss", 0),
                "day_pnl_pct": pos.get("currentDayProfitLossPercentage", 0),
                "total_pnl": pos.get("longOpenProfitLoss", 0) + pos.get("shortOpenProfitLoss", 0),
                "net_liq": account_info["net_liq"],
                "cash": account_info["cash"],
            })

    return all_positions


def get_quote(symbol: str) -> dict:
    """Get a current quote for a symbol."""
    data = _api_get(f"/marketdata/v1/quotes?symbols={symbol}")

    quote = data.get(symbol, {}).get("quote", {})
    return {
        "symbol": symbol,
        "last": quote.get("lastPrice"),
        "bid": quote.get("bidPrice"),
        "ask": quote.get("askPrice"),
        "change": quote.get("netChange"),
        "change_pct": quote.get("netPercentChangeInDouble"),
        "volume": quote.get("totalVolume"),
        "52w_high": quote.get("52WkHigh"),
        "52w_low": quote.get("52WkLow"),
    }


def get_quotes_batch(symbols: list[str]) -> dict[str, dict]:
    """
    Batch quote lookup — one API call for multiple symbols.
    Returns dict of symbol → quote data.
    """
    if not symbols:
        return {}
    sym_str = ",".join(s.strip() for s in symbols)
    try:
        data = _api_get(f"/marketdata/v1/quotes?symbols={sym_str}")
    except Exception as e:
        print(f"  [!] Batch quote failed: {e}")
        return {}

    result = {}
    for sym in symbols:
        quote = data.get(sym, {}).get("quote", {})
        if quote:
            result[sym] = {
                "symbol": sym,
                "last": quote.get("lastPrice"),
                "bid": quote.get("bidPrice"),
                "ask": quote.get("askPrice"),
                "change": quote.get("netChange"),
                "change_pct": quote.get("netPercentChangeInDouble"),
                "volume": quote.get("totalVolume"),
                "52w_high": quote.get("52WkHigh"),
                "52w_low": quote.get("52WkLow"),
            }
    return result


def _parse_period(period_str: str, frequency_str: str = "daily") -> dict:
    """
    Convert analyst shorthand → Schwab API params.
    period_str: '1w', '1m', '3m', '6m', '1y', etc.
    frequency_str: 'daily', 'weekly', 'monthly'
    Returns dict with periodType, period, frequencyType, frequency.
    """
    freq_map = {
        "daily": ("minute", 30) if False else ("daily", 1),
        "weekly": ("weekly", 1),
        "monthly": ("monthly", 1),
    }
    # Fix: daily is just daily/1
    freq_map = {
        "daily": {"frequencyType": "daily", "frequency": 1},
        "weekly": {"frequencyType": "weekly", "frequency": 1},
        "monthly": {"frequencyType": "monthly", "frequency": 1},
    }

    # Parse period string like "1m", "3m", "1y", "1w"
    if not period_str or len(period_str) < 2:
        return {}

    try:
        num = int(period_str[:-1])
    except ValueError:
        return {}

    unit = period_str[-1].lower()

    period_map = {
        "d": {"periodType": "day", "period": num},
        "w": {"periodType": "day", "period": num * 5},  # weeks → trading days
        "m": {"periodType": "month", "period": num},
        "y": {"periodType": "year", "period": num},
    }

    if unit not in period_map:
        return {}

    params = period_map[unit]
    params.update(freq_map.get(frequency_str, freq_map["daily"]))
    return params


def get_price_history(symbol: str, period: str = "1m",
                      frequency: str = "daily") -> dict:
    """
    Fetch price history from Schwab API.
    symbol: ticker (e.g. 'CF', '/NG')
    period: '1w', '1m', '3m', '6m', '1y'
    frequency: 'daily', 'weekly', 'monthly'
    Returns dict with 'symbol', 'candles' (list of {date, open, high, low, close, volume}),
    or empty dict on error.
    """
    params = _parse_period(period, frequency)
    if not params:
        print(f"  [!] Invalid period/frequency: {period}/{frequency}")
        return {}

    query = (
        f"/marketdata/v1/pricehistory"
        f"?symbol={symbol}"
        f"&periodType={params['periodType']}"
        f"&period={params['period']}"
        f"&frequencyType={params['frequencyType']}"
        f"&frequency={params['frequency']}"
    )

    try:
        data = _api_get(query)
    except Exception as e:
        print(f"  [!] Price history failed for {symbol}: {e}")
        return {}

    raw_candles = data.get("candles", [])
    candles = []
    for c in raw_candles:
        dt = datetime.fromtimestamp(c["datetime"] / 1000, tz=timezone.utc)
        candles.append({
            "date": dt.strftime("%Y-%m-%d"),
            "open": c.get("open"),
            "high": c.get("high"),
            "low": c.get("low"),
            "close": c.get("close"),
            "volume": c.get("volume"),
        })

    return {"symbol": symbol, "candles": candles}


def sync_portfolio() -> str:
    """
    Fetch positions and write portfolio.md.
    Returns the markdown content that was written.
    """
    positions = get_positions()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Portfolio",
        f"*Auto-synced from Schwab — last updated {now}*",
        "",
    ]

    if not positions:
        lines.append("## Current Positions")
        lines.append("*(No open positions)*")
        content = "\n".join(lines)
        PORTFOLIO_FILE.write_text(content)
        return content

    # Group by account
    accounts = {}
    for pos in positions:
        acct = pos["account"]
        if acct not in accounts:
            accounts[acct] = {
                "positions": [],
                "net_liq": pos["net_liq"],
                "cash": pos["cash"],
            }
        accounts[acct]["positions"].append(pos)

    for acct, info in accounts.items():
        lines.append(f"## Account ...{acct}")
        lines.append(f"- Net liquidation: ${info['net_liq']:,.2f}")
        lines.append(f"- Cash: ${info['cash']:,.2f}")
        lines.append("")
        lines.append("| Symbol | Type | Qty | Avg Price | Mkt Value | P&L |")
        lines.append("|--------|------|-----|-----------|-----------|-----|")

        for pos in sorted(info["positions"], key=lambda p: abs(p["market_value"]), reverse=True):
            symbol = pos["symbol"]
            asset = pos["asset_type"]
            qty = pos["quantity"]
            avg = pos["avg_price"]
            mkt = pos["market_value"]
            pnl = pos["total_pnl"]
            pnl_sign = "+" if pnl >= 0 else ""

            qty_str = f"{qty:.0f}" if qty == int(qty) else f"{qty:.4f}"
            lines.append(
                f"| {symbol} | {asset} | {qty_str} | ${avg:,.2f} | ${mkt:,.2f} | {pnl_sign}${pnl:,.2f} |"
            )

        lines.append("")

    lines.append("## Position Notes")
    lines.append("*(Add manual notes about position rationale here)*")

    content = "\n".join(lines)
    PORTFOLIO_FILE.write_text(content)
    print(f"Portfolio synced: {len(positions)} positions across {len(accounts)} account(s)")
    return content


# ── CLI ───────────────────────────────────────────────────────────────

def login():
    """First-time login — uses Playwright to authenticate and save token."""
    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")

    if not app_key or not app_secret:
        print("Missing SCHWAB_APP_KEY or SCHWAB_APP_SECRET in .env")
        return

    print("Authenticating with Schwab via auto-login...")
    try:
        code = _auto_login_playwright()
        token = _exchange_code_for_token(code)
        print(f"Authenticated! Token saved to {TOKEN_PATH}")
        print("Token will auto-refresh — no manual intervention needed.")
    except Exception as e:
        print(f"Auto-login failed: {e}")
        print("\nFalling back to manual flow...")
        print(f"1. Open this URL in your browser:")
        auth_url = f"{SCHWAB_AUTH_URL}?client_id={app_key}&redirect_uri={CALLBACK_URL}"
        print(f"   {auth_url}")
        print(f"2. Log in and authorize the app")
        print(f"3. Copy the full redirect URL and paste it here:")
        redirect_url = input("\nRedirect URL: ").strip()
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            token = _exchange_code_for_token(code)
            print(f"Authenticated! Token saved to {TOKEN_PATH}")
        else:
            print("No authorization code found in the URL.")


def cmd_show():
    """Dump raw position + account data so we can see all available fields."""
    accounts = get_account_hashes()
    print(f"\nFound {len(accounts)} account(s)\n")
    for acct in accounts:
        account_hash = acct["hashValue"]
        account_num = acct.get("accountNumber", "???")
        print(f"=== Account ...{account_num[-4:]} (raw API response) ===")
        data = _api_get(f"/trader/v1/accounts/{account_hash}?fields=positions")
        print(json.dumps(data, indent=2, default=str))
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 brokerage.py --login          # First-time authentication")
        print("  python3 brokerage.py --show           # Print raw positions data (no write)")
        print("  python3 brokerage.py --sync           # Sync positions to portfolio.md")
        print("  python3 brokerage.py --quote CF        # Get a quote")
        print("  python3 brokerage.py --history CF 1m daily  # Price history")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "--login":
        login()
    elif cmd == "--show":
        cmd_show()
    elif cmd == "--sync":
        content = sync_portfolio()
        print("\n--- portfolio.md ---")
        print(content)
    elif cmd == "--quote" and len(sys.argv) > 2:
        quote = get_quote(sys.argv[2])
        for k, v in quote.items():
            print(f"  {k}: {v}")
    elif cmd == "--history" and len(sys.argv) > 2:
        symbol = sys.argv[2]
        period = sys.argv[3] if len(sys.argv) > 3 else "1m"
        freq = sys.argv[4] if len(sys.argv) > 4 else "daily"
        result = get_price_history(symbol, period, freq)
        if result and result.get("candles"):
            print(f"\n{symbol} — {period} {freq} ({len(result['candles'])} candles)")
            for c in result["candles"][-10:]:  # Last 10
                print(f"  {c['date']}  O:{c['open']:.2f}  H:{c['high']:.2f}  L:{c['low']:.2f}  C:{c['close']:.2f}  V:{c['volume']:,}")
        else:
            print(f"No data for {symbol}")
    else:
        print(f"Unknown command: {cmd}")
