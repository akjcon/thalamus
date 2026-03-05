"""
Thalamus Analyst — Deep analysis with Sonnet when the scanner flags something.
Reads the full world model, researches follow-up questions, and produces
structured analysis with trade implications.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

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

    # Extract text from response, handling tool use blocks
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    return "\n".join(text_parts)


def validate_prices(client, trade_ideas: list[dict], model: str) -> list[dict]:
    """
    After trade ideas are formed, check if the instruments have already
    moved significantly. This is validation only — never idea generation.
    Returns updated trade ideas with price context added.
    """
    if not trade_ideas:
        return trade_ideas

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

        price_info = ""
        for block in response.content:
            if hasattr(block, "text"):
                price_info += block.text

        # Add price context to each idea
        for idea in trade_ideas:
            idea["price_check"] = price_info

    except Exception as e:
        print(f"  [!] Price validation failed: {e}")

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

    items_text = "\n\n".join(
        f"### {item['title']}\n"
        f"Source: {item['source']}\n"
        f"Why flagged: {item.get('what_is_new', item.get('reason', ''))}\n"
        f"Urgency: {item['urgency']}\n"
        f"Summary: {item.get('summary', 'N/A')}"
        for item in flagged_items
    )

    prompt = f"""## Current World Model
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
8. **Confidence** — given the counter-thesis, how confident are you? (low/medium/high)
9. **Portfolio impact** — how does this affect existing positions, if any?

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

## WRITING STYLE

The user is a smart trader but NOT a geopolitics expert. Write in plain English:
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
            "time_horizon": "weeks/months — be specific"
        }}
    ],
    "world_model_updates": "Markdown text describing what should be updated in the world model",
    "new_questions": ["questions to investigate in future cycles"],
    "alert_worthy": true/false
}}

Respond with ONLY the JSON, no other text."""

    response = client.messages.create(
        model=model,
        max_tokens=16384,
        system="""You are a geopolitical analyst focused on identifying non-obvious
second and third-order effects of world events. You think in supply chains,
commodity flows, and interconnected systems. You are intellectually honest
about uncertainty and always argue against your own thesis before presenting it.

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
        system="""You maintain a world model — a set of markdown files that represent
your current understanding of the geopolitical landscape. You are free to organize
these files however you want. Create new files, update existing ones, or restructure
as needed.

Output a JSON array of file operations:
[
    {"action": "write", "filename": "example.md", "content": "full file content"},
    {"action": "delete", "filename": "outdated.md"}
]

IMPORTANT RULES:
- ONLY include files that ACTUALLY CHANGED. Do NOT rewrite files that have no updates.
  If a file's content would be identical to what's already there, DO NOT include it.
- Only update _index.md if you added or removed files, or if section descriptions changed.
- Keep files concise — bullet points, not paragraphs. Capture key facts and dynamics.
- Filenames should be descriptive and use snake_case.
- One topic per file.
- Keep your total output SHORT. Fewer file operations = better. If the update only affects
  2 files, only output 2 operations. Do not rewrite the entire world model.
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

    # Truncation detection — if hit max_tokens, JSON is likely corrupt
    if response.stop_reason == "max_tokens":
        print(f"  [!] World model update hit max_tokens — output truncated, skipping to avoid corruption")
        return

    try:
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        operations = json.loads(text)
    except (json.JSONDecodeError, IndexError) as e:
        print(f"  [!] Failed to parse world model updates: {e}")
        raw = response.content[0].text if response.content else "(empty)"
        print(f"  [!] Response starts with: {raw[:500]}")
        print(f"  [!] Response ends with: {raw[-500:]}")
        print(f"  [!] Stop reason: {response.stop_reason}")
        return

    for op in operations:
        filepath = (WORLD_MODEL_DIR / op["filename"]).resolve()
        # Path traversal guard — must stay within WORLD_MODEL_DIR
        if not filepath.is_relative_to(WORLD_MODEL_DIR.resolve()):
            print(f"  [!] Path traversal blocked in world model update: {op['filename']}")
            continue
        if op["action"] == "write":
            filepath.write_text(op["content"])
            print(f"  [*] Updated world model: {op['filename']}")
        elif op["action"] == "delete" and filepath.exists():
            filepath.unlink()
            print(f"  [*] Removed from world model: {op['filename']}")
