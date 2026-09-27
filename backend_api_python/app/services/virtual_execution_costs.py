"""Deterministic execution-cost assumptions for signal-only virtual fills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.services.live_trading.capabilities import canonical_exchange_id, normalize_market_type


VIRTUAL_SLIPPAGE_RATE = 0.0005
VIRTUAL_COMMISSION_RATE = 0.0005


@dataclass(frozen=True)
class VirtualExecutionCostPolicy:
    exchange_id: str
    market_type: str
    leverage: float
    commission_rate: float
    slippage_rate: float = VIRTUAL_SLIPPAGE_RATE
    liquidity_role: str = "taker"

    def commission_for(self, *, quantity: float, fill_price: float) -> float:
        notional = max(0.0, float(quantity or 0.0)) * max(0.0, float(fill_price or 0.0))
        return notional * self.commission_rate

    def slippage_quote_for(
        self,
        *,
        quantity: float,
        reference_price: float,
        fill_price: float,
    ) -> float:
        return (
            max(0.0, float(quantity or 0.0))
            * abs(float(fill_price or 0.0) - float(reference_price or 0.0))
        )


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _positive_float(*values: Any, default: float = 1.0) -> float:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return float(default)


def resolve_virtual_execution_cost_policy(
    *,
    payload: Mapping[str, Any] | None = None,
    order_row: Mapping[str, Any] | None = None,
    strategy: Mapping[str, Any] | None = None,
    exchange_config: Mapping[str, Any] | None = None,
    trading_config: Mapping[str, Any] | None = None,
) -> VirtualExecutionCostPolicy:
    payload = payload or {}
    order_row = order_row or {}
    strategy = strategy or {}
    exchange_config = exchange_config or {}
    trading_config = trading_config or {}
    sizing = payload.get("sizing") if isinstance(payload.get("sizing"), Mapping) else {}

    exchange_id = canonical_exchange_id(_first_text(
        payload.get("exchange_id"),
        order_row.get("exchange_id"),
        trading_config.get("exchange_id"),
        exchange_config.get("exchange_id"),
    ))
    market_type = normalize_market_type(_first_text(
        payload.get("market_type"),
        order_row.get("market_type"),
        strategy.get("market_type"),
        trading_config.get("market_type"),
        exchange_config.get("market_type"),
        "spot",
    ))
    leverage = _positive_float(
        payload.get("leverage"),
        sizing.get("leverage"),
        strategy.get("leverage"),
        trading_config.get("leverage"),
        exchange_config.get("leverage"),
        default=1.0,
    )

    return VirtualExecutionCostPolicy(
        exchange_id=exchange_id,
        market_type=market_type,
        leverage=leverage,
        commission_rate=VIRTUAL_COMMISSION_RATE,
    )


__all__ = [
    "VIRTUAL_COMMISSION_RATE",
    "VIRTUAL_SLIPPAGE_RATE",
    "VirtualExecutionCostPolicy",
    "resolve_virtual_execution_cost_policy",
]
