"""
Thalamus Scanner — RSS ingest + Haiku novelty classification.
Pulls headlines from curated geopolitical news feeds and asks Haiku
whether anything represents a GENUINELY NEW development vs. routine updates.
"""

import feedparser
import yaml
import hashlib
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
MEMORY = ROOT / "memory"
SEEN_FILE = MEMORY / "seen_headlines.json"
WORLD_MODEL_DIR = MEMORY / "world_model"
ALERTS_DIR = MEMORY / "alerts"


def load_sources():
    with open(ROOT / "config" / "sources.yaml") as f:
        return yaml.safe_load(f)


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def hash_headline(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}:{link}".encode()).hexdigest()[:16]


def pull_feeds(sources: dict) -> list[dict]:
    """Pull all RSS feeds and return new (unseen) headlines."""
    seen = load_seen()
    new_headlines = []

    for feed_config in sources["rss_feeds"]:
        try:
            feed = feedparser.parse(feed_config["url"])
            for entry in feed.entries[:15]:  # Cap per source to avoid noise
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = entry.get("summary", "")[:500]
                h = hash_headline(title, link)

                if h not in seen:
                    seen[h] = datetime.now(timezone.utc).isoformat()
                    new_headlines.append({
                        "source": feed_config["name"],
                        "title": title,
                        "link": link,
                        "summary": summary,
                    })
        except Exception as e:
            print(f"  [!] Failed to fetch {feed_config['name']}: {e}")

    save_seen(seen)
    return new_headlines


def format_headlines_for_llm(headlines: list[dict]) -> str:
    """Format headlines into a compact string for the classifier."""
    lines = []
    for i, h in enumerate(headlines):
        lines.append(f"[{i}] ({h['source']}) {h['title']}")
        if h["summary"]:
            lines.append(f"    {h['summary'][:200]}")
    return "\n".join(lines)


def build_classifier_context() -> str:
    """Build compact context for the novelty classifier: index + recent tickers."""
    parts = []

    # World model index — tells the classifier what we already know
    index_file = WORLD_MODEL_DIR / "_index.md"
    if index_file.exists():
        parts.append(f"## What I Already Know (World Model Index)\n{index_file.read_text()}")

    # Recent trade tickers — so we don't re-flag the same instruments
    if ALERTS_DIR.exists():
        recent_tickers = set()
        for f in sorted(ALERTS_DIR.iterdir(), reverse=True)[:5]:
            try:
                alert = json.loads(f.read_text())
                for idea in alert.get("trade_ideas", []):
                    instrument = idea.get("instrument", "")
                    tickers = re.findall(r'\(([A-Z]{1,5})\)', instrument)
                    recent_tickers.update(tickers)
            except Exception:
                pass
        if recent_tickers:
            parts.append(f"## Recently Recommended Tickers\n{', '.join(sorted(recent_tickers))}")

    return "\n\n".join(parts)


def classify_headlines(client, headlines: list[dict], world_model_summary: str, model: str) -> list[dict]:
    """
    Ask Haiku to classify which headlines represent GENUINELY NEW developments
    vs. routine updates about situations we already track.
    Returns only headlines that warrant deeper analysis.
    """
    if not headlines:
        return []

    formatted = format_headlines_for_llm(headlines)
    classifier_context = build_classifier_context()

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system="""You are a geopolitical NOVELTY filter. Your job is NOT to decide if something
is "significant" — everything during a war is significant. Your job is to decide if a
headline tells us something we DON'T ALREADY KNOW.

You will be given:
1. The current world model index — what the analyst already tracks
2. Recently recommended tickers — trades already sent
3. New headlines to evaluate

YOUR QUESTION FOR EACH HEADLINE: "Does this CHANGE what I already know?"

SKIP (return nothing) for:
- Routine updates about an ongoing crisis we already track (e.g., "Iran war continues",
  "oil prices elevated", "shipping disrupted" — we know all this)
- New articles about the SAME situation with no material new information
- Casualties, damage reports, or operational updates that don't change the strategic picture
- Market commentary or analysis about situations already in the world model
- Headlines about instruments/tickers we already recommended

FLAG ONLY for:
- A NEW situation or actor not yet in the world model
- A MATERIAL CHANGE: escalation, de-escalation, ceasefire, new front, policy reversal
- A STRUCTURAL SHIFT: new sanctions, alliance change, infrastructure permanently damaged
- Something that would make an existing trade idea WRONG (invalidates a thesis)

During an active crisis like a regional war, 95% of headlines are routine updates.
That's expected. Return an empty array most of the time. Being silent is correct.""",
        messages=[{
            "role": "user",
            "content": f"""{classifier_context}

## New Headlines
{formatted}

Return a JSON array of objects for ONLY headlines that represent genuinely NEW information.
Each object should have:
- "index": the headline index number
- "what_is_new": one sentence explaining what SPECIFIC new information this adds (not "this is significant" — what is NEW?)
- "urgency": "high" (new crisis/major escalation/de-escalation), "medium" (material change to tracked situation), or "low" (new minor situation worth noting)
- "follow_up_questions": list of 1-3 questions to research further

If nothing is genuinely new, return an empty array: []

Respond with ONLY the JSON array, no other text."""
        }]
    )

    try:
        text = response.content[0].text.strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                inner = parts[1]
                if inner.startswith("json"):
                    inner = inner[4:]
                text = inner.strip()
            else:
                inner = parts[1]
                if inner.startswith("json"):
                    inner = inner[4:]
                text = inner.strip()

        flagged = json.loads(text)
    except (json.JSONDecodeError, IndexError) as e:
        print(f"  [!] Failed to parse classifier response (stop_reason={response.stop_reason}): {e}")
        print(f"  [!] First 300 chars: {response.content[0].text[:300]}")
        return []

    results = []
    for item in flagged:
        idx = item.get("index", -1)
        if 0 <= idx < len(headlines):
            results.append({
                **headlines[idx],
                "what_is_new": item.get("what_is_new", ""),
                "urgency": item.get("urgency", "low"),
                "follow_up_questions": item.get("follow_up_questions", []),
            })

    return results
