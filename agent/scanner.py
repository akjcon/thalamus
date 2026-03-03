"""
Thalamus Scanner — RSS ingest + Haiku classification.
Pulls headlines from curated geopolitical news feeds and asks Haiku
whether anything is significant enough to warrant deeper analysis.
"""

import feedparser
import yaml
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
MEMORY = ROOT / "memory"
SEEN_FILE = MEMORY / "seen_headlines.json"


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


def classify_headlines(client, headlines: list[dict], world_model_summary: str, model: str) -> list[dict]:
    """
    Ask Haiku to classify which headlines are geopolitically significant.
    Returns the list of headlines that warrant deeper analysis.
    """
    if not headlines:
        return []

    formatted = format_headlines_for_llm(headlines)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system="""You are a geopolitical intelligence classifier. Given a list of news headlines,
identify which ones are geopolitically significant — meaning they could have meaningful
second or third-order effects on global supply chains, commodity flows, international
relations, or security.

IGNORE: routine politics, market commentary, entertainment, sports, weather, human interest.
FLAG: conflicts, sanctions, trade policy, military movements, regime changes, infrastructure
disruptions, resource disputes, alliance shifts, election results with geopolitical implications.

For the current world model context, consider what the analyst is already tracking and
what questions are active.""",
        messages=[{
            "role": "user",
            "content": f"""## Current World Model Summary
{world_model_summary}

## New Headlines
{formatted}

Return a JSON array of objects for ONLY the significant headlines. Each object should have:
- "index": the headline index number
- "reason": one sentence on why this matters
- "urgency": "high" (new crisis/escalation), "medium" (developing situation), or "low" (worth noting)
- "follow_up_questions": list of 1-3 questions to research further

If nothing is significant, return an empty array: []

Respond with ONLY the JSON array, no other text."""
        }]
    )

    try:
        text = response.content[0].text.strip()
        # Handle markdown code blocks (```json ... ``` or ``` ... ```)
        if "```" in text:
            # Extract content between first ``` and last ```
            parts = text.split("```")
            if len(parts) >= 3:
                inner = parts[1]
                # Remove optional language tag (e.g., "json\n")
                if inner.startswith("json"):
                    inner = inner[4:]
                text = inner.strip()
            else:
                # Only opening ```, no closing — truncated response
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
                "reason": item.get("reason", ""),
                "urgency": item.get("urgency", "low"),
                "follow_up_questions": item.get("follow_up_questions", []),
            })

    return results
