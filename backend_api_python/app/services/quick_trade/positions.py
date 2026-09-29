"""Exchange position normalization for Quick Trade responses."""

from __future__ import annotations

from typing import Any

from app.services.live_trading.position_row_parse import (
    extract_signed_position_qty,
    infer_position_side_from_row,
)
from app.utils.logger import get_logger


logger = get_logger(__name__)


def normalize_okx_positions_raw(raw: Any) -> Any:
    """Attach explicit sides to signed OKX net-mode position rows."""
    if not isinstance(raw, dict):
        return raw
    data = raw.get("data")
    if not isinstance(data, list):
        return raw
    out_rows = []
    for item in data:
        if not isinstance(item, dict):
            out_rows.append(item)
            continue
        row = dict(item)
        position_side = str(row.get("posSide") or "").strip().lower()
        if position_side in ("long", "short"):
            row.setdefault("positionSide", position_side.upper())
        elif position_side == "net":
            signed = None
            for key in ("pos", "availPos", "posAmt"):
                try:
                    value = float(row.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if abs(value) > 1e-10:
                    signed = value
                    break
            if signed is not None:
                row["positionSide"] = "SHORT" if signed < 0 else "LONG"
        out_rows.append(row)
    out = dict(raw)
    out["data"] = out_rows
    return out


def parse_positions(raw: Any) -> list:
    """Best-effort parse positions from normalized or venue-native responses."""
    result = []
    if not raw:
        return result
    try:
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            if isinstance(raw.get("raw"), list):
                items = raw["raw"]
            else:
                data = raw.get("data") or raw.get("result") or raw.get("positions") or []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("list", []) if "list" in data else [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(
                item.get("symbol")
                or item.get("instId")
                or item.get("contract")
                or item.get("contract_code")
                or ""
            ).strip()
            display_symbol = symbol
            if symbol and "/" not in symbol:
                for separator in ("_", "-"):
                    if separator in symbol:
                        parts = symbol.split(separator, 1)
                        if len(parts) == 2 and parts[0] and parts[1]:
                            display_symbol = f"{parts[0]}/{parts[1]}"
                        break

            size = extract_signed_position_qty(item)
            explicit_side = str(
                item.get("positionSide") or item.get("position_side") or ""
            ).strip().upper()
            if explicit_side in ("LONG", "SHORT"):
                try:
                    amount = abs(float(item.get("positionAmt") or item.get("position_amt") or 0.0))
                except (TypeError, ValueError):
                    amount = 0.0
                if amount > 0:
                    size = amount if explicit_side == "LONG" else -amount
            if abs(size) < 1e-10:
                continue

            notional_usdt = 0.0
            for key in (
                "notionalUsd",
                "notional_usd",
                "notional",
                "positionValue",
                "position_value",
                "value",
            ):
                try:
                    candidate = abs(float(item.get(key) or 0.0))
                except (TypeError, ValueError):
                    candidate = 0.0
                if candidate > 0:
                    notional_usdt = candidate
                    break

            result.append(
                {
                    "symbol": display_symbol,
                    "side": infer_position_side_from_row(item),
                    "size": abs(size),
                    "entry_price": float(
                        item.get("entryPrice")
                        or item.get("entry_price")
                        or item.get("openPriceAvg")
                        or item.get("openAvgPrice")
                        or item.get("open_avg_price")
                        or item.get("avgEntryPrice")
                        or item.get("avgPrice")
                        or item.get("avgCost")
                        or item.get("avgPx")
                        or item.get("openAvgPx")
                        or item.get("accAvgPx")
                        or item.get("cost_open")
                        or item.get("trade_avg_price")
                        or 0
                    ),
                    "unrealized_pnl": float(
                        item.get("unRealizedProfit")
                        or item.get("unrealizedProfit")
                        or item.get("unrealizedPnl")
                        or item.get("unrealizedPL")
                        or item.get("unrealized_pnl")
                        or item.get("unrealized_profit")
                        or item.get("unrealised_pnl")
                        or item.get("upl")
                        or item.get("unrealisedPnl")
                        or item.get("profit_unreal")
                        or item.get("pnl")
                        or 0
                    ),
                    "leverage": float(
                        item.get("leverage")
                        or item.get("lever")
                        or item.get("lever_rate")
                        or item.get("cross_leverage_limit")
                        or 1
                    ),
                    "initial_margin": float(
                        item.get("initialMargin")
                        or item.get("initial_margin")
                        or item.get("positionInitialMargin")
                        or item.get("position_initial_margin")
                        or item.get("positionIM")
                        or item.get("positionIMByMp")
                        or item.get("marginSize")
                        or item.get("position_margin")
                        or item.get("imr")
                        or item.get("isolatedMargin")
                        or item.get("margin")
                        or 0
                    ),
                    "mark_price": float(
                        item.get("markPrice")
                        or item.get("mark_price")
                        or item.get("markPx")
                        or item.get("last_price")
                        or item.get("last")
                        or item.get("indexPrice")
                        or 0
                    ),
                    "notional_usdt": notional_usdt,
                }
            )
    except Exception as exc:
        logger.warning("Quick Trade position parse failed: %s", exc)
    return result


__all__ = [
    "extract_signed_position_qty",
    "infer_position_side_from_row",
    "normalize_okx_positions_raw",
    "parse_positions",
]
