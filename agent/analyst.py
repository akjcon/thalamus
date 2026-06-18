"""
Thalamus Analyst — Deep analysis with Sonnet when the scanner flags something.
Reads the full world model, researches follow-up questions, and produces
structured analysis with trade implications.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from costs import track as track_cost

ROOT = Path(__file__).parent.parent
MEMORY = ROOT / "memory"
WORLD_MODEL_DIR = MEMORY / "world_model"


def load_world_model() -> str:
    """Read all world model files and concatenate them."""
    if not WORLD_MODEL_DIR.exists():
        return "(World model is empty — first run.)"

    parts = []
    index_file = WORLD_MODEL_DIR / "_index.md"
    if index_file.exists():
        parts.append(f"## Index\n{index_file.read_text()}")

    for f in sorted(WORLD_MODEL_DIR.iterdir()):
        if f.name == "_index.md" or not f.suffix == ".md":
            continue
        parts.append(f"## {f.stem}\n{f.read_text()}")

    return "\n\n---\n\n".join(parts) if parts else "(World model is empty — first run.)"


def load_index() -> str:
    """Return just the generated _index.md (the macro map), not the full corpus.

    Used as lightweight macro context for the red-team validator, which does
    instrument-specific due diligence and does not need all per-topic files.
    """
    index_file = WORLD_MODEL_DIR / "_index.md"
    if index_file.exists():
        return index_file.read_text()
    return "(no world model index yet)"


def regenerate_index() -> None:
    """Deterministically (re)generate _index.md from the world model files.

    The index is a GENERATED artifact — the analyst no longer hand-maintains it,
    so it can never drift from the files it points to. Each entry shows the
    file's title, a short status/as-of descriptor, and when it was last updated
    (a freshness signal the analyst can use to spot stale topics).
    """
    if not WORLD_MODEL_DIR.exists():
        return

    files = [
        f for f in WORLD_MODEL_DIR.iterdir()
        if f.suffix == ".md" and f.name != "_index.md"
    ]
    # Most recently updated first — surfaces what's live and flags what's gone stale.
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    lines = [
        "# World Model Index",
        "",
        "_Auto-generated after each update — do not edit by hand; manual edits are overwritten._",
        "",
        f"{len(files)} topic files, most recently updated first.",
        "",
    ]
    for f in files:
        text = f.read_text()
        title = f.stem
        status = ""
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
        for line in text.splitlines():
            low = line.strip().lower()
            if low.startswith("**status:") or low.startswith("**as of:"):
                status = re.sub(r"\*+", "", line.strip()).strip()
                break
        updated = datetime.fromtimestamp(
            f.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        desc = f" — {status[:160]}" if status else ""
        lines.append(f"- **[{f.name}]({f.name})** (updated {updated}) — {title}{desc}")

    (WORLD_MODEL_DIR / "_index.md").write_text("\n".join(lines) + "\n")


def load_portfolio() -> str:
    """Read current portfolio."""
    portfolio_file = MEMORY / "portfolio.md"
    if portfolio_file.exists():
        return portfolio_file.read_text()
    return "(No portfolio tracked yet.)"


def research_questions(client, questions: list[str], model: str) -> str:
    """
    Use Claude's web search capabilities to research follow-up questions.
    Returns a summary of findings.
    """
    if not questions:
        return ""

    questions_text = "\n".join(f"- {q}" for q in questions)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{
            "role": "user",
            "content": f"""Research the following questions and provide factual findings.
Focus on primary sources, government data, and reputable reporting.
Do NOT include market analysis or financial commentary.

Questions:
{questions_text}

For each question, provide:
1. What you found
2. Key data points or facts
3. Source credibility assessment"""
        }]
    )

    track_cost("research", response, model)

    # Extract text from response, handling tool use blocks
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    return "\n".join(text_parts)


def _fetch_schwab_price_data(trade_ideas: list[dict]) -> tuple[str, bool]:
    """
    Collect price_queries from trade ideas, fetch from Schwab API.
    Returns (formatted price data string, success bool).
    """
    try:
        from brokerage import get_price_history, get_quotes_batch
    except (ImportError, Exception) as e:
        print(f"  [!] Brokerage import failed: {e}")
        return "", False

    # Collect all unique symbols and their queries
    all_queries = []
    all_symbols = set()
    for idea in trade_ideas:
        for pq in idea.get("price_queries", []):
            sym = pq.get("symbol", "").strip()
            if sym:
                all_symbols.add(sym)
                all_queries.append(pq)

    # If no price_queries specified, extract tickers from instrument names
    if not all_symbols:
        import re
        for idea in trade_ideas:
            instrument = idea.get("instrument", "")
            tickers = re.findall(r'\(([A-Z/]{1,10})\)', instrument)
            all_symbols.update(tickers)

    if not all_symbols:
        return "", False

    lines = []
    success_count = 0

    # Batch quotes for current prices
    try:
        quotes = get_quotes_batch(list(all_symbols))
        for sym, q in quotes.items():
            last = q.get("last")
            change_pct = q.get("change_pct")
            volume = q.get("volume")
            high52 = q.get("52w_high")
            low52 = q.get("52w_low")
            if last is not None:
                pct_str = f"{change_pct:+.2f}%" if change_pct is not None else "n/a"
                vol_str = f"{volume:,}" if volume else "n/a"
                if high52 is not None and low52 is not None:
                    range_str = f"52w range: ${low52:.2f}-${high52:.2f}"
                else:
                    range_str = "52w range: n/a"
                lines.append(
                    f"**{sym}** — Last: ${last:.2f} | Day change: {pct_str} | "
                    f"Volume: {vol_str} | {range_str}"
                )
                success_count += 1
    except Exception as e:
        print(f"  [!] Batch quotes failed: {e}")

    # Price history for each query
    for pq in all_queries:
        sym = pq.get("symbol", "").strip()
        period = pq.get("period", "1m")
        freq = pq.get("frequency", "daily")
        try:
            hist = get_price_history(sym, period, freq)
            candles = hist.get("candles", [])
            if candles and len(candles) >= 2:
                first_close = candles[0]["close"]
                last_close = candles[-1]["close"]
                period_change = ((last_close - first_close) / first_close) * 100
                high = max(c["high"] for c in candles)
                low = min(c["low"] for c in candles)
                avg_vol = sum(c["volume"] for c in candles if c["volume"]) / len(candles)
                lines.append(
                    f"**{sym}** ({period} {freq}): {first_close:.2f} → {last_close:.2f} "
                    f"({period_change:+.1f}%) | Range: {low:.2f}-{high:.2f} | "
                    f"Avg volume: {avg_vol:,.0f}"
                )
                success_count += 1
        except Exception as e:
            print(f"  [!] Price history for {sym} failed: {e}")

    if not lines:
        return "", False

    return "\n".join(lines), success_count > 0


def validate_prices(client, trade_ideas: list[dict], model: str) -> list[dict]:
    """
    After trade ideas are formed, check if the instruments have already
    moved significantly. Tries Schwab API first (free), falls back to
    web_search if Schwab is unavailable.
    Returns updated trade ideas with price context, filtering ideas
    where the move already happened.
    """
    if not trade_ideas:
        return trade_ideas

    # Try Schwab API first
    price_data, schwab_ok = _fetch_schwab_price_data(trade_ideas)

    if schwab_ok and price_data:
        # Schwab path: structured data → single Sonnet call, no web_search
        ideas_text = "\n\n".join(
            f"**{i+1}. {idea.get('direction', '').upper()} {idea.get('instrument', '')}**\n"
            f"Thesis: {idea.get('one_liner', '')}"
            for i, idea in enumerate(trade_ideas)
        )

        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{
                    "role": "user",
                    "content": f"""Here are trade ideas and their price data. For each idea, determine
if the expected move has ALREADY happened (priced in) or if there's still opportunity.

## Trade Ideas
{ideas_text}

## Price Data
{price_data}

For each idea, output one of:
- KEEP — the move hasn't happened yet or is early
- DROP — the move already happened, it's priced in
- REVISE — the data suggests a modification

Output JSON:
{{
    "decisions": [
        {{"index": 1, "action": "KEEP|DROP|REVISE", "reason": "brief explanation", "price_context": "1-2 sentences of key price facts"}}
    ]
}}

Respond with ONLY the JSON."""
                }]
            )
            track_cost("price_check", response, model)

            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(text)

            # Apply decisions
            surviving = []
            decisions = result.get("decisions", [])
            for i, idea in enumerate(trade_ideas):
                decision = next((d for d in decisions if d.get("index") == i + 1), None)
                if decision:
                    action = decision.get("action", "KEEP").upper()
                    if action == "DROP":
                        print(f"  [*] Dropping idea {i+1}: {decision.get('reason', '')}")
                        continue
                    idea["price_context"] = decision.get("price_context", "")
                    if action == "REVISE":
                        idea["price_context"] += f" (Note: {decision.get('reason', '')})"
                surviving.append(idea)

            return surviving

        except (json.JSONDecodeError, IndexError, KeyError) as e:
            print(f"  [!] Price validation parse error: {e}")
            # Attach raw price data and return all ideas
            for idea in trade_ideas:
                idea["price_context"] = price_data
            return trade_ideas

        except Exception as e:
            print(f"  [!] Price validation Sonnet call failed: {e}")
            for idea in trade_ideas:
                idea["price_context"] = price_data
            return trade_ideas

    # Fallback: web_search (Schwab unavailable)
    print("  [*] Schwab unavailable — falling back to web_search for price validation")
    tickers = []
    for idea in trade_ideas:
        instrument = idea.get("instrument", "")
        tickers.append(instrument)

    query = "Current price and this week's percentage change for: " + ", ".join(tickers)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{
                "role": "user",
                "content": f"""Look up the current price and recent price movement (past 5 days)
for these instruments. Just give me the facts — current price, 5-day change,
and whether there's been unusual volume or movement.

{query}

Be brief. One line per instrument."""
            }]
        )

        track_cost("price_check_fallback", response, model)

        price_info = ""
        for block in response.content:
            if hasattr(block, "text"):
                price_info += block.text

        for idea in trade_ideas:
            idea["price_context"] = price_info

    except Exception as e:
        print(f"  [!] Price validation fallback failed: {e}")

    return trade_ideas


def load_recent_alerts(n: int = 3) -> str:
    """Load recent alert trade ideas so the analyst knows what was already recommended."""
    alerts_dir = MEMORY / "alerts"
    if not alerts_dir.exists():
        return "(No previous alerts)"
    alert_files = sorted(alerts_dir.iterdir(), reverse=True)[:n]
    lines = []
    for f in alert_files:
        try:
            import json as _json
            alert = _json.loads(f.read_text())
            ts = f.stem  # timestamp from filename
            ideas = alert.get("trade_ideas", [])
            if ideas:
                idea_strs = [f"  - {i.get('direction','').upper()} {i.get('instrument','')}: {i.get('one_liner','')}" for i in ideas]
                lines.append(f"**{ts}** (alert_worthy={alert.get('alert_worthy', False)}):\n" + "\n".join(idea_strs))
            else:
                lines.append(f"**{ts}**: No trade ideas (quiet cycle)")
        except Exception:
            pass
    return "\n\n".join(lines) if lines else "(No previous alerts)"


def deep_analysis(client, flagged_items: list[dict], world_model: str,
                  portfolio: str, research: str, model: str) -> dict:
    """
    Run deep analysis on flagged items. Returns structured analysis
    with trade implications.
    """
    recent_alerts = load_recent_alerts(n=10)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    items_text = "\n\n".join(
        f"### {item['title']}\n"
        f"Source: {item['source']}\n"
        f"Why flagged: {item.get('what_is_new', item.get('reason', ''))}\n"
        f"Urgency: {item['urgency']}\n"
        f"Summary: {item.get('summary', 'N/A')}"
        for item in flagged_items
    )

    prompt = f"""Today is {today}.

## Current World Model
{world_model}

## Current Portfolio
{portfolio}

## Recent Alerts (already sent to user)
{recent_alerts}

## Flagged Events
{items_text}

## Research Findings
{research}

---

Analyze these events for second and third-order effects. Think step by step:

1. **What happened?** State the event plainly.
2. **First-order effects** — the obvious, direct consequences. Note these but DO NOT trade on them.
3. **Second-order effects** — what do the first-order effects cause?
4. **Third-order effects** — what do those cause? This is where non-obvious trades live.
5. **Supply chain implications** — what commodity flows, shipping routes, or input costs are affected DOWNSTREAM?
6. **Tradeable implications** — what specific instruments could move, and in what direction?
7. **Counter-thesis** — argue against your own conclusion. What would invalidate this?
8. **Assumption audit** — List the 2-3 assumptions this thesis depends on. For each: how confident are you, and what evidence would break it? If any assumption is "the market hasn't priced this in," replace it with a structural reason you have an edge.
9. **"What would change my mind?"** — Name one specific, observable thing that would falsify this thesis in the next 2 weeks. Not a vague risk — a concrete signal you could check.
10. **Confidence** — given the counter-thesis AND the assumption audit, how confident are you? (low/medium/high)
11. **Portfolio impact** — how does this affect existing positions, if any?
12. **Reconcile with your world model** — For each event, state how it relates to what you
    already believe: does it CONFIRM, CONTRADICT, or EXTEND an existing thesis? If it
    CONTRADICTS, name the file and the stale claim so it gets fixed (e.g. "qatar_crisis.md
    still says LNG restarts in 3-6 weeks; this implies Q1 2027"). Which OTHER topics in your
    world model does this connect to? An event in one domain that shifts your thesis in
    another (Iran → fertilizer → corn; shipping → consumer goods) is exactly the cross-topic
    edge — say so explicitly. If a dated prediction's deadline has already passed (today is
    {today}) without the predicted event, mark it WRONG/PARTIAL and de-escalate rather than
    silently re-dating it forward.

## CRITICAL RULES FOR TRADE IDEAS

**FILTER OUT OBVIOUS TRADES.** If a trade idea would appear on the front page of
Bloomberg or CNBC — if it's what every retail investor and headline reader is already
thinking — it is already priced in and you MUST NOT suggest it. Examples of obvious
trades you should NEVER suggest:
- "Long oil because Middle East conflict" — everyone sees this
- "Short airline stocks because oil is up" — first-order, consensus
- "Long defense stocks because war" — priced in within hours

**YOUR VALUE IS IN THE CONNECTIONS OTHERS AREN'T MAKING.** Think 2-3 steps
downstream from the headline. The best trade ideas look like:
- "Iran conflict → Hormuz disruption → fertilizer shipping halted → input costs spike
  for spring planting → long July wheat futures" (3rd order, weeks-to-months horizon)
- "Qatar LNG halt → European industrial gas rationing → specific chemical companies
  that rely on gas feedstock get crushed → short X" (2nd-3rd order)
- "Sanctions enforcement surge → shadow fleet insurance pulled → specific tanker
  operators benefit from compliant fleet premium → long X" (structural shift)

**PREFER LONGER HORIZON TRADES.** The user trades on weeks-to-months timeframes,
not intraday reactions. The best ideas are ones where:
- The market hasn't priced in the downstream effect yet
- The causal chain takes time to propagate through the real economy
- You're early to a structural shift, not chasing a headline spike
- Input cost changes take weeks to flow through to end products

For each trade idea, ask yourself: "Would a smart person who only reads headlines
come up with this?" If yes, discard it and dig deeper.

**DO NOT REPEAT TRADE IDEAS — BY CONCEPT, NOT JUST TICKER.** Check the "Recent Alerts"
section above. If you already recommended a trade based on a particular thesis or causal
chain, do NOT recommend another trade on the same CONCEPT — even with a different ticker.
For example: if you already alerted on CF Industries because of the nitrogen/TTF spread,
do NOT then alert on Bunge, Yara, or corn futures for the same fertilizer supply chain
disruption. Same thesis = same alert, regardless of instrument. Only alert again if there
is a genuinely new development that MATERIALLY changes the thesis — a new event, not
just "more articles about the same situation." If the situation is playing out as
expected, that's confirmation, not a new alert. Update the world model quietly.

**ONLY SUGGEST INSTRUMENTS TRADEABLE THROUGH A US BROKERAGE (Schwab, Fidelity, etc.).**
The user trades through a standard US brokerage account. Only suggest instruments that are
readily accessible:
- US-listed stocks and ETFs (NYSE, NASDAQ, AMEX)
- US-listed ADRs of foreign companies (e.g., "TotalEnergies (TTE)" not "Paris-listed TotalEnergies")
- Major US futures (CME, NYMEX, CBOT, ICE US) — e.g., WTI crude, Henry Hub, wheat, soybeans
- US-listed options on any of the above

Do NOT suggest:
- Foreign-listed stocks (Vienna, London, Tokyo, Shanghai, etc.)
- Foreign futures exchanges (ICE Europe, SGX, LME) unless the same contract trades on a US exchange
- Instruments that require special international brokerage access
- Forex pairs (the user doesn't trade FX)
- Crypto

If the best trade idea involves a foreign company, find the US-listed ADR or a US-listed
ETF with heavy exposure to that theme. If no US-accessible instrument exists, note the
thesis in the world model but do NOT send it as an alert.

**MOST CYCLES SHOULD HAVE ZERO TRADE IDEAS.** Only suggest a trade when you have
genuine conviction — a clear, non-obvious chain of reasoning where you can explain
each link. Having no trade ideas is the normal, expected outcome. Do not pad.
Set "alert_worthy" to false unless you have a trade idea with at least medium
confidence. A quiet update to the world model is the default.

**PRICE VALIDATION — CHECK AFTER, NEVER BEFORE.** The direction of reasoning is
ALWAYS: geopolitical event → analysis → trade idea → THEN check the price.
NEVER: price moved → why? → narrative → trade idea.
Prices should only be used to validate/invalidate a trade idea AFTER you've formed
it from first principles. If you form a thesis on CF Industries and then discover
it's already up 20% this week, that's useful — the move may be priced in. But you
should NEVER look at asset prices to generate ideas. You are a geopolitical analyst,
not a quant.

**POSITION OVERLAP — HARD FILTER**
Before recommending ANY trade, check the Current Portfolio above. If the portfolio
already has exposure that profits from the same fundamental thesis, SUPPRESS the idea.
Do not include it in trade_ideas at all.

This applies even if the instruments are different:
- Portfolio has long /NG → do NOT recommend long CF, UAN, or any nat-gas-input company
- Portfolio has long XOM → do NOT recommend long CVX, OXY, or any crude oil play
- Portfolio has short EUR/USD → do NOT recommend long DXY or short European equities for
  the same macro reason

The test: "If the thesis behind my existing position plays out as expected, would this new
trade also profit for the same reason?" If yes → suppress.

Exception: A genuinely DISTINCT second-order effect that traces to a different causal
mechanism may survive. But "different ticker, same bet" is not distinct. Be ruthless here —
when in doubt, suppress.

For each trade idea you DO include, add an "overlap_check" field explaining why it is NOT
redundant with existing positions.

## TRADE ALERT QUALITY FILTERS

Before including ANY trade idea, apply these filters sequentially. If any filter fails,
discard the idea entirely. Do NOT include it in trade_ideas.

**1. Instrument-Thesis Tightness Test**
Ask: "Is there a direct, mechanistic link between the catalyst and THIS specific
company's revenue or margins — or does the thesis require 3+ inferential hops?"
- PASS: Hormuz closes → fertilizer supply destroyed → CF's cheap US gas feedstock
  creates widening margin advantage (2 hops, each mechanistic and quantifiable)
- FAIL: War → Dubai flights disrupted → Indian pharma logistics collapse → US
  injectable shortage → ICUI pricing power (4 hops, each uncertain)
- Rule: Maximum 2 causal hops between catalyst and earnings impact. Each hop must be
  supported by a quantifiable supply/demand mechanism, not a narrative.

**2. Stale Company Data Check**
Before alerting on any ticker, verify:
- Has the company divested, spun off, or restructured the relevant business segment
  in the last 18 months?
- Does the current revenue mix actually match the thesis? Base this on the most recent
  10-K/10-Q, not historical descriptions.
- Rule: Never alert on a company based on a business line it no longer operates.

**3. "Is There a Tighter Instrument?" Test**
For every alert, ask: "Is there another publicly traded company or futures contract
that captures this exact thesis with fewer assumptions?"
- If yes, alert on that instrument instead.
- If the tighter instrument doesn't exist, that's a signal the thesis may be too
  niche to trade.

**4. Substitution and Rerouting Analysis**
For any supply chain disruption thesis, explicitly check:
- Can the disrupted flow reroute? (e.g., air freight via Singapore instead of Dubai)
- Can the affected buyers substitute from other sources? (e.g., US LNG terminals
  already at max capacity means Henry Hub doesn't follow TTF)
- Is the disrupted channel actually the primary channel? (e.g., Indian pharma moves
  mostly by ocean, not air through Dubai)
- Rule: If rerouting or substitution can neutralize the disruption within the trade's
  timeframe, do not alert.

**5. "Already Priced In" Check**
Assess how obvious the thesis is:
- Is the catalyst already front-page news?
- Has the stock already moved significantly on the catalyst?
- Are sell-side analysts already publishing on this exact thesis?
- Rule: If the thesis is consensus, the edge is gone. Only alert on second/third-order
  effects that mainstream coverage hasn't connected yet.

**6. Risk/Reward Asymmetry Check**
Before alerting, assess:
- What is the downside if the thesis is wrong? (e.g., shorting a beaten-down stock
  has poor risk/reward)
- Is the stock already priced for distress? (e.g., a company that lost $738M last
  year doesn't have much further to fall)
- Is the position exposed to headline reversal risk? (e.g., a single ceasefire
  headline could wipe the trade)
- Rule: Only alert when the upside on the thesis being correct meaningfully exceeds
  the downside of being wrong.

**7. Timeline Specificity Requirement**
Every alert must include:
- A specific catalyst with a date or date range (e.g., "USDA crop report May 12"
  not "6-10 weeks")
- A defined exit trigger if the thesis fails
- Rule: If you cannot name a specific upcoming event that will confirm or deny the
  thesis, the idea is too vague to trade.

For each trade idea, add a "quality_filter_notes" field — a brief sentence confirming
it passed the above filters (e.g., "2 causal hops, no substitution path, catalyst is
OPEC meeting June 1").

## SLOW-FUSE SUPPLY SHOCKS — PRIME THE THESIS BEFORE THE TRIGGER

A recurring high-value archetype: a slow-moving biological or physical supply threat
advancing toward a market with NO inventory buffer, ending in a discrete,
government-confirmed trigger event. (Worked example: New World screwworm marching north
2022-2026 toward a US cattle herd at 75-year lows — knowable ~18 months early from import
suspensions and USDA emergency capex; by the first-US-case headline the move was priced.)

Three components to check:
1. **The fuse** — deterministic spread you can track via dated proximity signals: geographic
   advance, government import suspensions/quarantines, emergency eradication funding. Each
   official action is the government telling you its own internal model.
2. **The buffer** — the terminal market's inventory slack. THIS IS THE MULTIPLIER. A threat
   advancing on a record-tight market (multi-decade-low herds/stocks) reprices violently; the
   same threat against ample inventory is noise. Always check the buffer before flagging.
3. **The trigger** — the confirmable headline event everyone watches ("first US case",
   "production halt confirmed"). By trigger time the story is consensus.

When you spot such a fuse EARLY: write a world-model file NOW with the thesis, the natural
expression (usually the deferred end of the futures curve), the proximity ladder, and a
PRE-ARMED TRIGGER line — "on <specific event>, thesis VALIDATED; do NOT initiate on the
confirmation headline — that is the exit-liquidity event, not the entry." Scheduled
government reports (USDA NASS Cattle inventory late Jan/Jul, Cattle on Feed monthly 3rd
Friday, WASDE monthly, crop forecasts) are free, datable confirmation points for the buffer
condition. The edge is being positioned (or primed) before the trigger, never reacting to it.

**Calibrate the fuse — a trigger firing is not a detonation.** A pre-armed trigger firing
means the thesis is LIVE and worth acting on — NOT that the threat is irreversibly
"established", "confirmed", or "structural". Keep your status language proportional to what
is actually verified. An active, funded containment response (quarantines, import bans,
sterile-fly / eradication / vaccination programs) cuts BOTH ways: it confirms the authorities
take the threat seriously, but it is also the force that can still DEFEAT your thesis — do
NOT score containment effort as thesis confirmation. Reserve "established / structural /
multi-year destruction confirmed" for when the threat has actually reached the
inventory-critical market itself (e.g. the feedlot belt, not border counties), OR a sustained
containment failure (the file's own de-escalation clock has run). Until then the honest status
is "advancing / plausible", and the trade is "armed", not "activated".
- NO jargon without explanation. Don't say "TTF" — say "European natural gas prices (TTF)"
- NO acronyms without defining them. Don't say "VLCC" — say "large oil tankers (VLCCs)"
- Each step in the chain should explain WHY, not just WHAT. Not "European fertilizer
  curtailment" but "European fertilizer factories shut down because gas is their main
  ingredient and it's now too expensive to run"
- The chain should read like you're explaining it to a friend over coffee
- Keep the instrument field simple — include the ticker and what it actually is

Then produce your output as JSON with this structure:
{{
    "events_analyzed": ["short plain-english descriptions, not raw headlines"],
    "situation_summary": "2-3 sentences max. Plain English. What is happening in the world right now that matters. No jargon.",
    "analysis_narrative": "Your full analysis as markdown text",
    "trade_ideas": [
        {{
            "instrument": "what to trade — plain name + ticker (e.g., 'CF Industries (CF) — US fertilizer company')",
            "direction": "long or short",
            "one_liner": "~15 words max explaining WHY this trade works, for scanning at a glance",
            "thesis": "one paragraph explaining why in plain English",
            "chain": ["step 1 — explain why this causes step 2", "step 2", "step 3", "step 4"],
            "order": "2nd/3rd — label which order effect this trade captures",
            "counter_thesis": "what could make this wrong, in plain English",
            "confidence": "low/medium/high",
            "time_horizon": "weeks/months — be specific",
            "key_assumptions": ["assumption 1 — the load-bearing belief", "assumption 2"],
            "invalidation_signal": "one specific, observable thing to watch that would kill this thesis in the next 2 weeks",
            "exit_trigger": "specific condition or date that kills the thesis — when to get out",
            "catalyst_date": "specific upcoming event + date that confirms/denies thesis (e.g., 'USDA crop report May 12')",
            "quality_filter_notes": "brief confirmation this idea passed all 7 quality filters",
            "overlap_check": "why this is NOT redundant with existing portfolio positions",
            "price_queries": [
                {{"symbol": "CF", "period": "1m", "frequency": "daily"}},
                {{"symbol": "/NG", "period": "3m", "frequency": "weekly"}}
            ]
        }}
    ],
    "world_model_updates": "Markdown describing what to update — for each fact note CONFIRM/CONTRADICT/EXTEND, name any file with a contradicting value that must be fixed, and flag any expired prediction to de-escalate. Do not just append news.",
    "new_questions": ["questions to investigate in future cycles"],
    "alert_worthy": true/false
}}

**price_queries explained:** For each trade idea, specify 1-3 symbols you want price history
for so we can validate whether the move has already happened. Use the main instrument ticker
plus any key related instruments (e.g., an input commodity). Available periods: 1w, 1m, 3m,
6m, 1y. Frequencies: daily, weekly, monthly. Use daily for shorter periods, weekly for 6m+.

Respond with ONLY the JSON, no other text."""

    response = client.messages.create(
        model=model,
        max_tokens=16384,
        system="""You are a geopolitical and supply-chain analyst focused on identifying
non-obvious second and third-order effects of world events. You think in supply chains,
commodity flows, and interconnected systems. Biological and agricultural supply shocks —
animal disease, crop disease, climate stress, herd/inventory cycles — are first-class
event sources for you, not just downstream effects of geopolitics. You are intellectually
honest about uncertainty and always argue against your own thesis before presenting it.

You do NOT read market commentary or financial news. You reason from
first principles about how events propagate through real-world systems.

Your edge is DEPTH, not speed. You are not a news alert service. You find the
trades that take weeks to play out — where an event today causes an input cost
shift that won't hit earnings or futures prices for 2-8 weeks. Think about how
disruptions propagate through physical supply chains: shipping times, inventory
drawdowns, planting seasons, contract rollovers, procurement cycles.

IMPORTANT: Keep your JSON output concise. Focus on the 2-3 most non-obvious
trade ideas rather than exhaustively covering every event. Be brief in
narratives — bullet points over paragraphs. If you can't find a genuinely
non-obvious trade, say so — don't pad with consensus ideas.""",
        messages=[{"role": "user", "content": prompt}]
    )
    track_cost("deep_analysis", response, model)

    try:
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except (json.JSONDecodeError, IndexError) as e:
        return {
            "error": f"Failed to parse analysis: {e}",
            "raw_response": response.content[0].text[:2000],
            "alert_worthy": False,
        }


def _extract_final_text(response_content) -> str:
    """Text blocks after the last non-text block. With server-side web_search,
    the model emits search calls interleaved with text; the final answer lives
    after the last tool/search block."""
    last_non_text_idx = -1
    for i, block in enumerate(response_content):
        if not hasattr(block, "text"):
            last_non_text_idx = i
    if last_non_text_idx >= 0:
        return "\n".join(
            b.text for b in response_content[last_non_text_idx + 1:] if hasattr(b, "text")
        )
    return "\n".join(b.text for b in response_content if hasattr(b, "text"))


def _parse_json_object(text: str) -> dict | None:
    """Pull the first complete JSON object out of a string. Tolerates markdown
    fences and surrounding prose. Returns None if no object parses."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 3:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.strip()
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        return json.loads(text[first:last + 1])
    except json.JSONDecodeError:
        return None


def red_team_validate(client, idea: dict, world_model: str, portfolio: str, model: str) -> dict:
    """
    Adversarial validation of a single trade idea. Default verdict is KILL —
    only CONFIRM when web_search evidence forces it.

    Investigates with primary-source research: revenue exposure, contract/
    hedging structure, magnitude vs. recent guidance, tighter-instrument
    alternatives, priced-in status. Returns CONFIRM/KILL/REVISE with reasoning.

    On JSON parse failure, returns verdict="ERROR" so the caller can pass the
    idea through with a warning rather than silently suppress.
    """
    instrument = idea.get("instrument", "")
    direction = idea.get("direction", "").upper()
    thesis = idea.get("thesis", "")
    chain = idea.get("chain", [])
    counter = idea.get("counter_thesis", "")
    assumptions = idea.get("key_assumptions", [])
    time_horizon = idea.get("time_horizon", "")
    confidence = idea.get("confidence", "")
    invalidation = idea.get("invalidation_signal", "")

    chain_text = "\n".join(f"  {i+1}. {step}" for i, step in enumerate(chain)) or "  (none)"
    assumptions_text = "\n".join(f"  - {a}" for a in assumptions) or "  (none)"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    system_prompt = """You are a SKEPTICAL EQUITY ANALYST tasked with KILLING a trade idea before it gets sent to a user. A geopolitical analyst has proposed the trade. Your job is to find reasons it WILL NOT WORK on the specific instrument proposed.

KEY INSIGHT: The geopolitical thesis is usually right at the INDUSTRY level. The failure mode is usually that the specific company proposed does not actually capture the upside — because its revenue mix, contract structure, hedging, or magnitude of impact insulates it from the catalyst.

YOUR DEFAULT VERDICT IS KILL. Only CONFIRM when positive evidence shows the specific instrument will materially benefit from the thesis within the proposed time horizon.

## What to investigate

Use web_search aggressively — pull as many threads as you need. There is no cap on how deep you go. Verify with PRIMARY SOURCES (10-K, 10-Q, earnings calls, company press releases, government data) — not market commentary blogs.

1. **Revenue exposure.** What percentage of the company's revenue actually correlates with the thesis catalyst? Has the company divested, spun off, or restructured the relevant business in the last 18 months? If the thesis depends on a business line, verify it still exists and is material to current revenue mix.

2. **Contract / inventory / hedging structure.** Will the catalyst flow through to earnings within the proposed time horizon?
   - Shipping: long-term charters vs. spot exposure. A fleet on multi-year charters at fixed rates does NOT capture a spot scarcity premium for years.
   - Producers: forward hedging programs that lock in old prices.
   - Chemicals/refining: lag between input-cost change and earnings impact.
   - Traders/distributors: pass-through margins vs. absolute price exposure.

3. **Magnitude check.** Compare the thesis-implied move to recent guidance and realized prices.
   - Most recent earnings call: has management raised guidance? By how much?
   - A 5-15% guidance raise on a thesis that requires 30-50% repricing is a FAIL.
   - If the thesis claims "structural shift" but guidance, realized prices, or volumes are unchanged → KILL.

4. **Tighter instrument check.** Is there a US-tradeable alternative that captures the SAME thesis with less dilution?
   - The user trades through a standard US brokerage (Schwab). Alternatives must be US-listed stocks/ETFs/ADRs or major US futures (CME/NYMEX/CBOT/ICE US).
   - If a tighter alternative exists, the proposed instrument loses → KILL or REVISE.

5. **Already priced in.** Has the stock already moved on this thesis? Check 1m/3m/6m price action. Is sell-side already publishing on this exact thesis?

## Output format

After investigation, output ONLY this JSON object (no preamble, no postamble, no markdown fences):

{
  "verdict": "CONFIRM" | "KILL" | "REVISE",
  "verdict_confidence": "low" | "medium" | "high",
  "exposure_check": "what % of revenue / operations actually ties to the thesis, with citation",
  "structure_check": "contract / hedging / inventory structure — will it flow through in the time horizon? with citation",
  "magnitude_check": "compare thesis-implied magnitude to recent guidance / realized prices, with citation",
  "tighter_instrument": "ticker + one-line why, or 'none found'",
  "key_findings": ["specific fact with source URL", "specific fact with source URL"],
  "kill_reasons": ["reason 1 (empty list if verdict is CONFIRM)"],
  "final_reasoning": "one paragraph explaining the verdict",
  "revision_suggestion": "if REVISE, what specifically to change; otherwise empty string"
}

VERDICT RULES:
- CONFIRM: All five investigation areas pass with positive evidence. No tighter instrument exists. Thesis is not already priced in.
- KILL: Any one of revenue exposure / contract structure / magnitude fails materially, OR a tighter instrument exists, OR thesis is already priced in.
- REVISE: The macro thesis is right but the specific instrument, direction, or time horizon needs adjustment (e.g., "right thesis, wrong ticker — use X instead")."""

    user_prompt = f"""## Trade Idea to Validate (today is {today})

**Direction:** {direction}
**Instrument:** {instrument}
**Time horizon:** {time_horizon}
**Confidence claimed by analyst:** {confidence}

**Thesis:**
{thesis}

**Causal chain:**
{chain_text}

**Counter-thesis (analyst's own):**
{counter}

**Key assumptions:**
{assumptions_text}

**Invalidation signal:**
{invalidation}

## World Model Index (macro map; the full per-topic files are NOT included here — rely on web_search for instrument specifics)
{world_model}

## Current Portfolio
{portfolio}

---

Investigate using web_search. Verify with primary sources. Pull as many threads as you need — go deep. Then output the JSON verdict described in the system prompt."""

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 30}],
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    label_instrument = instrument.split("(")[0].strip()[:20] or "unknown"
    track_cost(f"red_team[{label_instrument}]", response, model)

    raw_text = _extract_final_text(response.content)
    parsed = _parse_json_object(raw_text)

    if parsed is None:
        return {
            "verdict": "ERROR",
            "verdict_confidence": "low",
            "final_reasoning": "Validator output did not contain parseable JSON — surfacing for human review.",
            "key_findings": [],
            "kill_reasons": [],
            "tighter_instrument": "n/a",
            "exposure_check": "n/a",
            "structure_check": "n/a",
            "magnitude_check": "n/a",
            "revision_suggestion": "",
            "raw_response": raw_text[:2000],
            "parse_error": True,
        }

    return parsed


def save_validation(timestamp: str, idea: dict, validation: dict) -> Path:
    """Persist a red-team validation result for audit / over-filter spot-checks."""
    val_dir = MEMORY / "validations"
    val_dir.mkdir(parents=True, exist_ok=True)
    instrument = idea.get("instrument", "unknown")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", instrument)[:40] or "unknown"
    filepath = val_dir / f"{timestamp}_{safe}.json"
    payload = {
        "timestamp": timestamp,
        "idea": idea,
        "validation": validation,
    }
    filepath.write_text(json.dumps(payload, indent=2))
    return filepath


def update_world_model(client, current_model: str, updates: str, model: str):
    """
    Let the agent update its own world model. It decides what files to
    create, modify, or reorganize.
    """
    # Truncate updates if massive to stay within reasonable context
    if len(updates) > 20000:
        updates = updates[:20000] + "\n\n[... truncated for length]"

    response = client.messages.create(
        model=model,
        max_tokens=32768,
        timeout=600.0,
        system="""You maintain a world model — a set of markdown files representing your
current understanding of the geopolitical landscape. Your job each cycle is to RECONCILE
new information into this model, not to pile it on top. A world model is a living
understanding, not a news log.

Output a JSON array of file operations:
[
    {"action": "write", "filename": "example.md", "content": "full file content"},
    {"action": "delete", "filename": "outdated.md"}
]

## RECONCILE — DECIDE, DON'T APPEND
For each piece of new information, decide which it is and act accordingly:
- CONFIRM — it matches what a file already says. Update that file IN PLACE (refresh the
  "as of" date, tighten wording). Do NOT spawn a new file or restate it elsewhere.
- CONTRADICT — it conflicts with what a file already says. REPLACE the stale claim and say
  so, e.g. "Prior estimate (August 2026) superseded — now Q1 2027." Never leave two files
  asserting different values for the same fact.
- EXTEND — it adds something genuinely new to an existing topic. Revise that file in place.

## A FACT LIVES IN EXACTLY ONE FILE
If a date, number, or status is relevant to several files, keep it in the single most
relevant owner file and REFERENCE that file elsewhere (e.g. "see oil_gas_market.md") —
never copy the value into multiple files, because copies drift and contradict.

## DE-ESCALATE WHEN REALITY DISAGREES
If a dated prediction's deadline has passed without the predicted event, mark it WRONG or
PARTIAL and DOWNGRADE the alarm — do NOT silently re-date it forward. A prediction restated
every cycle as "still 4-8 weeks away" is a failure mode. You are explicitly permitted and
expected to lower confidence and de-escalate when a prior call didn't pan out.
De-escalation is NOT only for expired dates: also downgrade when a file's STATUS or CERTAINTY
language outruns the evidence — e.g. a file marked "CONFIRMED / ESTABLISHED / irreversible /
structural" while the situation is still actively contested or being fought (active
containment response, threat not yet in the inventory-critical market). Re-rate the status
DOWN to match what is actually verified. Over-certainty is the same failure mode as a
prediction perpetually "4-8 weeks away".

## OTHER RULES
- ONLY include files that ACTUALLY CHANGED. If a file's content would be identical, omit it.
- Prefer revising an existing file over creating a new one. Before creating a new file,
  confirm none of the existing files already covers this theme — if one does, update it.
- Do NOT write _index.md — it is generated automatically from the files. Any _index.md
  operation you emit will be ignored.
- Use the "delete" action to retire files that are fully superseded or no longer worth tracking.
- Keep files concise — bullet points, not paragraphs. Filenames are descriptive snake_case.
- Keep total output SHORT. Fewer operations = better. Do not rewrite the whole world model.
- HARD CAP: Maximum 5 file operations per cycle. If more than 5 need updating, pick the 5
  most important (fixing a contradiction counts) and skip the rest.
- Maximum 800 words per file. Be ruthlessly concise — key facts and dynamics only.
- Each file should end with a `## Watch For` section — 2-3 open questions or specific signals
  to monitor next cycle. These feed back as context, creating a self-questioning loop.
  Examples: "Watch for: whether Hormuz insurance premiums spike above X", "Watch for: Q2
  fertilizer guidance on input-cost pass-through." If nothing is worth watching, the topic
  may not be worth a file.
Respond with ONLY the JSON array.""",
        messages=[{
            "role": "user",
            "content": f"""## Current World Model
{current_model}

## Updates to incorporate
{updates}

Apply these updates to the world model. Create, modify, or reorganize files as needed."""
        }]
    )

    track_cost("world_model_update", response, model)

    # Truncation detection — if hit max_tokens, JSON is likely corrupt
    if response.stop_reason == "max_tokens":
        print(f"  [!] World model update hit max_tokens — output truncated, skipping to avoid corruption")
        return

    try:
        text = response.content[0].text
        # Extract the outermost JSON array, tolerating code fences OR a prose
        # preamble (Sonnet sometimes prepends "Looking at the updates..." despite
        # the "ONLY the JSON array" instruction). A stray sentence must NOT silently
        # drop the entire world-model update — that's a known staleness cause.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end <= start:
            raise json.JSONDecodeError("no JSON array found in response", text, 0)
        operations = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, IndexError) as e:
        print(f"  [!] Failed to parse world model updates: {e}")
        raw = response.content[0].text if response.content else "(empty)"
        print(f"  [!] Response starts with: {raw[:500]}")
        print(f"  [!] Response ends with: {raw[-500:]}")
        print(f"  [!] Stop reason: {response.stop_reason}")
        # Persist so a silent update failure is distinguishable from a quiet cycle.
        try:
            err_dir = MEMORY / "errors"
            err_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            (err_dir / f"world_model_update_{ts}.json").write_text(json.dumps({
                "stage": "world_model_update",
                "error": str(e),
                "stop_reason": response.stop_reason,
                "raw_response": raw[:4000],
            }, indent=2))
        except Exception:
            pass
        return

    for op in operations:
        filepath = (WORLD_MODEL_DIR / op["filename"]).resolve()
        # Path traversal guard — must stay within WORLD_MODEL_DIR
        if not filepath.is_relative_to(WORLD_MODEL_DIR.resolve()):
            print(f"  [!] Path traversal blocked in world model update: {op['filename']}")
            continue
        # The index is a generated artifact — ignore any model op that touches it.
        if filepath.name == "_index.md":
            print(f"  [*] Ignored model op on generated _index.md")
            continue
        if op["action"] == "write":
            filepath.write_text(op["content"])
            print(f"  [*] Updated world model: {op['filename']}")
        elif op["action"] == "delete" and filepath.exists():
            filepath.unlink()
            print(f"  [*] Removed from world model: {op['filename']}")

    # Rebuild the index from whatever files now exist so it can never drift.
    regenerate_index()
