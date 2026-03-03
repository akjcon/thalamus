"""
Thalamus Seed — run a one-time initial world model build.
Asks the analyst to create an initial geopolitical picture
based on current events via web search.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic
from analyst import update_world_model, WORLD_MODEL_DIR

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT = Path(__file__).parent.parent


def run_web_search_loop(client, model, tools, messages, max_tokens=8192):
    """
    Run a message loop that handles web search tool use and pause_turn.
    The API may return intermediate tool_use / pause_turn responses
    that need to be fed back in to continue.
    """
    while True:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            messages=messages,
        )

        # If the model is done, return the final response
        if response.stop_reason == "end_turn":
            return response

        # If paused or needs to continue after tool use, feed it back
        if response.stop_reason in ("pause_turn", "tool_use"):
            # Append the assistant's partial response
            messages.append({"role": "assistant", "content": response.content})
            # For tool_use, we need to provide results — but web search is
            # server-side so we just continue with an empty user turn
            messages.append({"role": "user", "content": "Continue."})
        else:
            # Unknown stop reason, just return what we have
            return response


def seed_world_model():
    """Build the initial world model from scratch using web search."""
    client = Anthropic()

    print("Seeding initial world model via web search...\n")

    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]
    messages = [{
        "role": "user",
        "content": """You are building an initial geopolitical world model for a trading
intelligence system. Research the current state of the world and produce a comprehensive
overview of:

1. Active conflicts and military tensions
2. Major sanctions regimes and trade disputes
3. Critical infrastructure / shipping / supply chain situations
4. Upcoming elections or political transitions with geopolitical implications
5. Resource and energy supply dynamics
6. Key alliance relationships under stress

Focus on GEOPOLITICAL FACTS, not market reactions. Do not include stock prices,
market commentary, or financial analysis.

For each situation, note:
- What's happening
- Key actors involved
- What commodities or supply chains are affected
- Whether the situation is escalating, stable, or de-escalating
- What to watch for next

Be thorough. This will serve as the foundation for ongoing monitoring."""
    }]

    response = run_web_search_loop(client, "claude-sonnet-4-6", tools, messages)

    # Extract the text response
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    initial_picture = "\n".join(text_parts)

    print(f"Initial research complete ({len(initial_picture)} chars). Building world model files...\n")

    # Let the agent organize this into its own file structure
    current_model = "(Empty — first run.)"
    update_world_model(client, current_model, initial_picture, "claude-sonnet-4-6")

    print("\nWorld model seeded. Check memory/world_model/ to review.")


if __name__ == "__main__":
    seed_world_model()
