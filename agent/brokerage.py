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
TOKEN_PATH = Path(os.environ.get("SCHWAB_TOKEN_PATH", str(ROOT / ".schwab_token.json")))

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


def _auto_login_playwright() -> str:
    """
    Use Playwright to automate the Schwab OAuth login flow.
    Returns the authorization code from the redirect URL.
    """
    from playwright.sync_api import sync_playwright

    app_key = os.environ.get("SCHWAB_APP_KEY")
    username = os.environ.get("SCHWAB_USERNAME")
    password = os.environ.get("SCHWAB_PASSWORD")

    if not username or not password:
        raise ValueError("SCHWAB_USERNAME and SCHWAB_PASSWORD must be set in .env for auto-login")

    auth_url = f"{SCHWAB_AUTH_URL}?client_id={app_key}&redirect_uri={CALLBACK_URL}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("  Auto-login: navigating to Schwab...")
        page.goto(auth_url)
        page.wait_for_load_state("networkidle")

        # Fill login form
        print("  Auto-login: entering credentials...")
        page.fill("#loginIdInput", username)
        page.fill("#passwordInput", password)
        page.click("#btnLogin")

        # Wait for redirect or 2FA page
        # Schwab may show a "terms" or "authorize" page
        page.wait_for_load_state("networkidle", timeout=30000)

        # Check if we need to accept/authorize
        try:
            accept_btn = page.locator("text=Accept", timeout=5000)
            if accept_btn.is_visible():
                print("  Auto-login: accepting authorization...")
                accept_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # No accept button, that's fine

        # Try to find the "Allow" button for OAuth consent
        try:
            allow_btn = page.locator("#acceptTerms, text=Allow, text=Authorize", timeout=5000)
            if allow_btn.is_visible():
                print("  Auto-login: granting access...")
                allow_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # Wait for redirect to callback URL
        max_wait = 30
        for _ in range(max_wait):
            current_url = page.url
            if CALLBACK_URL.split("//")[1] in current_url:
                break
            time.sleep(1)
        else:
            # Capture what we see for debugging
            screenshot_path = ROOT / ".schwab_login_debug.png"
            page.screenshot(path=str(screenshot_path))
            browser.close()
            raise RuntimeError(
                f"Auto-login did not redirect to callback URL within {max_wait}s. "
                f"Current URL: {page.url}. "
                f"Screenshot saved to {screenshot_path} for debugging. "
                "This could be a CAPTCHA, 2FA prompt, or changed login page."
            )

        # Extract auth code from redirect URL
        redirect_url = page.url
        browser.close()

    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]

    if not code:
        raise RuntimeError(f"No auth code in redirect URL: {redirect_url}")

    print("  Auto-login: got authorization code")
    return code


def ensure_valid_token() -> dict:
    """
    Get a valid token, auto-refreshing or re-authenticating as needed.
    This is the main entry point — call this before any API request.
    """
    token = _load_token()

    if token:
        # Check if refresh token is about to expire (>6 days)
        if _token_needs_refresh(token):
            print("  Refresh token expiring soon — re-authenticating...")
            try:
                code = _auto_login_playwright()
                token = _exchange_code_for_token(code)
                print("  Token refreshed via auto-login")
                return token
            except Exception as e:
                print(f"  [!] Auto-login failed: {e}")
                # Try using existing refresh token as fallback
                pass

        # Try refreshing the access token (it expires every 30 min)
        try:
            token = _refresh_access_token(token)
            return token
        except Exception as e:
            print(f"  Access token refresh failed: {e}")
            # Try full re-auth
            try:
                code = _auto_login_playwright()
                token = _exchange_code_for_token(code)
                return token
            except Exception as e2:
                raise RuntimeError(f"All auth methods failed: {e2}")

    # No token at all — need to authenticate
    code = _auto_login_playwright()
    token = _exchange_code_for_token(code)
    return token


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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 brokerage.py --login          # First-time authentication")
        print("  python3 brokerage.py --sync           # Sync positions to portfolio.md")
        print("  python3 brokerage.py --quote CF        # Get a quote")
        print("  python3 brokerage.py --history CF 1m daily  # Price history")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "--login":
        login()
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
