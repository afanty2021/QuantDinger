"""Quick Trade history response helpers."""

from __future__ import annotations

import json
from typing import Any


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_quick_trade_metadata(raw_result: Any) -> dict[str, Any]:
    """Extract stable history fields from stored execution metadata."""
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except (TypeError, ValueError):
            raw_result = {}
    candidate = raw_result.get("_quick_trade") if isinstance(raw_result, dict) else {}
    metadata = candidate if isinstance(candidate, dict) else {}
    return {
        "margin_mode": metadata.get("margin_mode") or "",
        "requested_base_qty": _number(metadata.get("requested_base_qty")),
        "notional_usdt": _number(metadata.get("notional_usdt")),
        "amount_semantics": metadata.get("amount_semantics") or "",
        "client_order_id": metadata.get("client_order_id") or "",
    }
