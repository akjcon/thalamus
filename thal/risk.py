"""Hard limits, enforced in plain code outside any model.

Every order passes through check() before a broker sees it. The model can propose
anything; only what fits these limits is placed. Live trading needs two switches:
mode: live in the config file AND THAL_LIVE=yes in the environment.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "risk.yaml"


@dataclass(frozen=True)
class Limits:
    mode: str = "paper"                       # paper | live
    max_position_usd: float = 2_000.0         # per symbol, absolute value
    max_gross_usd: float = 20_000.0           # sum of absolute position values
    max_positions: int = 15
    max_orders_per_day: int = 10
    allowed_asset_classes: tuple[str, ...] = ("equity",)
    allow_short: bool = False
    blocked_symbols: tuple[str, ...] = ()
    kill_switch: str = str(ROOT / "data" / "KILL")   # if this file exists, nothing trades

    def __post_init__(self):
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be paper or live, got {self.mode!r}")
        for name in ("max_position_usd", "max_gross_usd", "max_positions", "max_orders_per_day"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} must be a non-negative number, got {v!r}")
        if self.max_position_usd > self.max_gross_usd:
            raise ValueError("max_position_usd cannot exceed max_gross_usd")


def load(path: str | Path = DEFAULT_PATH) -> Limits:
    """Read limits from YAML. A missing file means the safe defaults; a bad file is an error."""
    path = Path(path)
    if not path.exists():
        return Limits()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")
    known = {f.name for f in fields(Limits)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown risk settings: {sorted(unknown)}")   # a typo must not silently fall back
    for k in ("allowed_asset_classes", "blocked_symbols"):
        if k in raw:
            raw[k] = tuple(raw[k])
    return Limits(**raw)


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)   # symbol -> signed quantity
    prices: dict[str, float] = field(default_factory=dict)      # symbol -> last price

    def value(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0) * self.prices.get(symbol, 0.0)

    @property
    def gross(self) -> float:
        return sum(abs(self.value(s)) for s in self.positions)

    @property
    def equity(self) -> float:
        return self.cash + sum(self.value(s) for s in self.positions)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    action: str            # BUY | SELL
    qty: float
    price: float           # reference price used for the checks
    asset_class: str = "equity"


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


def check(order: OrderRequest, portfolio: Portfolio, limits: Limits, orders_today: int = 0,
          env: dict | None = None) -> Decision:
    env = os.environ if env is None else env
    if Path(limits.kill_switch).exists():
        return Decision(False, ("kill switch is on",))
    reasons: list[str] = []
    if limits.mode == "live" and env.get("THAL_LIVE") != "yes":
        reasons.append("live mode needs THAL_LIVE=yes in the environment")
    if order.action not in ("BUY", "SELL"):
        reasons.append(f"unknown action {order.action!r}")
    ok_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
    if not ok_num(order.qty):
        reasons.append("quantity must be a positive number")
    if not ok_num(order.price):
        reasons.append("no valid reference price")
    if reasons:
        return Decision(False, tuple(reasons))

    held = portfolio.positions.get(order.symbol, 0.0)
    after = held + (order.qty if order.action == "BUY" else -order.qty)
    reducing = abs(after) < abs(held) and (after == 0 or (after > 0) == (held > 0))
    if reducing:
        return Decision(True)          # getting smaller is always allowed (short of the kill switch)

    if order.symbol in limits.blocked_symbols:
        reasons.append(f"{order.symbol} is blocked")
    if order.asset_class not in limits.allowed_asset_classes:
        reasons.append(f"asset class {order.asset_class!r} is not allowed")
    if after < 0 and not limits.allow_short:
        reasons.append("short positions are not allowed")
    if orders_today >= limits.max_orders_per_day:
        reasons.append(f"daily order limit of {limits.max_orders_per_day} reached")
    new_value = abs(after) * order.price
    if new_value > limits.max_position_usd + 1e-9:
        reasons.append(f"position would be ${new_value:,.0f}, over the ${limits.max_position_usd:,.0f} per-position limit")
    gross_after = portfolio.gross - abs(held) * portfolio.prices.get(order.symbol, order.price) + new_value
    if gross_after > limits.max_gross_usd + 1e-9:
        reasons.append(f"gross exposure would be ${gross_after:,.0f}, over the ${limits.max_gross_usd:,.0f} limit")
    open_after = {s for s, q in portfolio.positions.items() if q != 0} | {order.symbol}
    if len(open_after) > limits.max_positions:
        reasons.append(f"would hold {len(open_after)} positions, over the limit of {limits.max_positions}")
    if order.action == "BUY" and order.qty * order.price > portfolio.cash + 1e-9:
        reasons.append("not enough cash")
    return Decision(not reasons, tuple(reasons))
