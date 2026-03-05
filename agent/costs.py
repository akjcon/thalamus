"""
Thalamus Cost Tracker — per-call API cost tracking with cycle summaries.
Prices are per 1M tokens as of March 2026.
"""

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

_cost_log: list[dict] = []


def track(label: str, response, model: str):
    """Extract usage from an Anthropic response, compute cost, log it."""
    usage = response.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens

    pricing = MODEL_PRICING.get(model, {"input": 0, "output": 0})
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

    # Short model name for display
    short = model.split("-")[1] if "-" in model else model

    entry = {
        "label": label,
        "model": model,
        "short_model": short,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
    }
    _cost_log.append(entry)
    print(f"  [cost] {label}: {input_tokens:,}in/{output_tokens:,}out = ${cost:.3f} ({short})")


def reset():
    """Clear the cost log for a new cycle."""
    _cost_log.clear()


def get_log() -> list[dict]:
    """Return the current cost log."""
    return list(_cost_log)


def total_cost() -> float:
    """Sum all costs in the current log."""
    return sum(e["cost"] for e in _cost_log)


def format_cycle_summary() -> str:
    """Format the cost log as a Discord message."""
    if not _cost_log:
        return "**Scan cycle: $0.00** (0 calls)"

    lines = [f"**Scan cycle: ${total_cost():.2f}** ({len(_cost_log)} calls)"]
    for e in _cost_log:
        lines.append(
            f"- {e['label']}: {e['input_tokens']:,}in/{e['output_tokens']:,}out "
            f"= ${e['cost']:.3f} ({e['short_model']})"
        )
    return "\n".join(lines)
