"""One door to the models: role -> model, structured output, cost log, daily budget.

Every call is written to the runs table (the searchable transcript layer) with its
token counts and cost. A call that would start after the daily budget is spent raises
BudgetExceeded instead of spending.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-fable-5-2", "claude-opus-5-5")   # models whose refusals can be re-routed server-side
DAY = 86400.0


class BudgetExceeded(RuntimeError):
    pass


class Refused(RuntimeError):
    pass


def load_env(path: str | Path = ROOT / ".env") -> None:
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class LLM:
    def __init__(self, conn: sqlite3.Connection, client=None, config: dict | None = None):
        self.conn = conn
        self.cfg = config or yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic
            load_env()
            self._client = anthropic.Anthropic()
        return self._client

    def spent_today(self, now: float | None = None) -> float:
        now = now or time.time()
        return self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM runs WHERE at >= ?",
                                 (now - now % DAY,)).fetchone()[0]

    def cost(self, model: str, usage) -> float:
        p_in, p_out, p_read = self.cfg["prices"][model]
        g = lambda k: getattr(usage, k, 0) or 0
        return (g("input_tokens") * p_in + g("cache_creation_input_tokens") * p_in * 1.25
                + g("cache_read_input_tokens") * p_read + g("output_tokens") * p_out) / 1e6

    def parse(self, role: str, system: str, user: str, schema: type[BaseModel], now: float | None = None) -> BaseModel:
        """Ask the role's model for an answer that validates against schema."""
        now = now or time.time()
        r = self.cfg["roles"][role]
        budget = float(self.cfg.get("daily_budget_usd", 0))
        if budget and self.spent_today(now) >= budget:
            raise BudgetExceeded(f"daily budget of ${budget:.2f} is spent; skipping {role}")
        kwargs = dict(model=r["model"], max_tokens=r["max_tokens"], output_config={"effort": r["effort"]},
                      system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                      messages=[{"role": "user", "content": user}], output_format=schema)
        # Streamed, because the SDK refuses large non-streaming requests that could outlast its timeout.
        if r["model"] in FALLBACK_MODELS:      # a safety-classifier decline is re-run on a fallback model
            manager = self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            manager = self.client.messages.stream(**kwargs)
        with manager as stream:
            resp = stream.get_final_message()
        cost = self.cost(resp.model if resp.model in self.cfg["prices"] else r["model"], resp.usage)
        out = resp.parsed_output
        u = resp.usage
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(at, kind, model, input, output, in_tok, out_tok, cache_read, cost) VALUES (?,?,?,?,?,?,?,?,?)",
                (now, role, resp.model, user, out.model_dump_json() if out is not None else f"[{resp.stop_reason}]",
                 (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                 getattr(u, "output_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0, cost))
        if resp.stop_reason == "refusal":
            raise Refused(f"{role} call was declined")
        if resp.stop_reason == "max_tokens" or out is None:
            raise RuntimeError(f"{role} call returned no valid answer (stop_reason={resp.stop_reason})")
        return out

    def turn(self, role: str, system: str, messages: list, tools: list, now: float | None = None):
        """One step of a tool-using session. Returns the raw response; the caller owns the loop."""
        now = now or time.time()
        r = self.cfg["roles"][role]
        budget = float(self.cfg.get("daily_budget_usd", 0))
        if budget and self.spent_today(now) >= budget:
            raise BudgetExceeded(f"daily budget of ${budget:.2f} is spent; stopping {role}")
        kwargs = dict(model=r["model"], max_tokens=r["max_tokens"], output_config={"effort": r["effort"]},
                      system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                      messages=messages, tools=tools)
        if r["model"] in FALLBACK_MODELS:
            manager = self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default",
                                                       cache_control={"type": "ephemeral"}, **kwargs)
        else:
            manager = self.client.messages.stream(**kwargs)
        with manager as stream:
            resp = stream.get_final_message()
        u = resp.usage
        cost = self.cost(resp.model if resp.model in self.cfg["prices"] else r["model"], u)
        said = " ".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        calls = [f"{b.name}({b.input})" for b in resp.content if getattr(b, "type", "") == "tool_use"]
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(at, kind, model, input, output, in_tok, out_tok, cache_read, cost) VALUES (?,?,?,?,?,?,?,?,?)",
                (now, role, resp.model, f"turn {len(messages)}", (said + "\n" + "\n".join(calls)).strip(),
                 (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                 getattr(u, "output_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0, cost))
        return resp
