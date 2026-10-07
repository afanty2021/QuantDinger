"""Build bounded, point-in-time evidence for pre-trade AI decisions."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from app.data_sources import DataSourceFactory
from app.services.kline import KlineService
from app.services.market.technical_indicators import calculate_indicators
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _rounded(value: Any, digits: int = 6) -> float | None:
    number = _number(value)
    return round(number, digits) if number is not None else None


def _timestamp_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    try:
        result = float(value)
        return result / 1000.0 if result > 10_000_000_000 else result
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _frame_rows(frame: Any, limit: int = 120) -> list[dict[str, Any]]:
    if frame is None or bool(getattr(frame, "empty", True)):
        return []
    try:
        subset = frame.tail(limit).copy()
        rows: list[dict[str, Any]] = []
        for index, row in subset.iterrows():
            item = row.to_dict()
            item.setdefault("time", index)
            rows.append(item)
        return rows
    except Exception as exc:
        logger.debug("AI decision frame conversion skipped: %s", exc)
        return []


def _clean_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows or ():
        close = _number(row.get("close"))
        high = _number(row.get("high"), close)
        low = _number(row.get("low"), close)
        open_price = _number(row.get("open"), close)
        if close is None or close <= 0 or high is None or low is None:
            continue
        output.append({
            "time": row.get("time") or row.get("timestamp") or row.get("datetime"),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": _number(row.get("volume") or row.get("vol"), 0.0),
        })
    return output


def _change(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-bars - 1] <= 0:
        return None
    return (closes[-1] / closes[-bars - 1] - 1.0) * 100.0


def _timeframe_seconds(timeframe: str) -> int | None:
    value = str(timeframe or "").strip().lower()
    if len(value) < 2:
        return None
    try:
        amount = int(value[:-1])
    except ValueError:
        return None
    multiplier = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(value[-1])
    return amount * multiplier if amount > 0 and multiplier else None


def summarize_market_bars(
    rows: Iterable[Mapping[str, Any]],
    *,
    timeframe: str,
    reference_price: float = 0.0,
) -> dict[str, Any]:
    clean = _clean_rows(rows)
    if len(clean) < 5:
        return {"timeframe": timeframe, "available": False, "bars": len(clean)}
    closes = [float(row["close"]) for row in clean]
    latest = clean[-1]
    technical = calculate_indicators(clean)
    latest_ts = _timestamp_seconds(latest.get("time"))
    age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - latest_ts) if latest_ts else None
    interval_seconds = _timeframe_seconds(timeframe)
    stale_after_seconds = interval_seconds * 3 if interval_seconds else None
    moving = technical.get("moving_averages") or {}
    volatility = technical.get("volatility") or {}
    rsi = technical.get("rsi") or {}
    macd = technical.get("macd") or {}
    levels = technical.get("levels") or {}
    current = float(latest["close"])
    return {
        "timeframe": timeframe,
        "available": True,
        "bars": len(clean),
        "latest_bar_time_utc": (
            datetime.fromtimestamp(latest_ts, tz=timezone.utc).isoformat() if latest_ts else None
        ),
        "data_age_seconds": _rounded(age_seconds, 1),
        "interval_seconds": interval_seconds,
        "stale_after_seconds": stale_after_seconds,
        "is_stale": bool(
            age_seconds is not None
            and stale_after_seconds is not None
            and age_seconds > stale_after_seconds
        ),
        "last_close": _rounded(current),
        "reference_price_deviation_pct": _rounded(
            ((float(reference_price) / current) - 1.0) * 100.0 if reference_price > 0 else None,
            3,
        ),
        "return_1_bar_pct": _rounded(_change(closes, 1), 3),
        "return_5_bar_pct": _rounded(_change(closes, 5), 3),
        "return_20_bar_pct": _rounded(_change(closes, 20), 3),
        "trend": technical.get("trend") or moving.get("trend") or "unknown",
        "ma5": _rounded(moving.get("ma5")),
        "ma10": _rounded(moving.get("ma10")),
        "ma20": _rounded(moving.get("ma20")),
        "rsi14": _rounded(rsi.get("value"), 2),
        "rsi_state": rsi.get("signal") or "unknown",
        "macd_state": macd.get("signal") or "unknown",
        "macd_histogram": _rounded(macd.get("histogram")),
        "atr14": _rounded(volatility.get("atr")),
        "atr14_pct": _rounded(volatility.get("pct"), 3),
        "volume_ratio_20": _rounded(technical.get("volume_ratio"), 3),
        "price_position_20": _rounded(technical.get("price_position"), 2),
        "support": _rounded(levels.get("support")),
        "resistance": _rounded(levels.get("resistance")),
    }


def _decision_timeframes(primary: str) -> list[str]:
    normalized = str(primary or "").strip().lower() or "15m"
    ladders = {
        "1m": ["1m", "15m", "1h"],
        "3m": ["3m", "15m", "1h"],
        "5m": ["5m", "15m", "1h"],
        "15m": ["15m", "1h", "4h"],
        "30m": ["30m", "1h", "4h"],
        "1h": ["1h", "4h", "1d"],
        "2h": ["2h", "4h", "1d"],
        "4h": ["4h", "1d", "1w"],
        "1d": ["1d", "1w"],
        "1w": ["1w", "1d"],
    }
    return ladders.get(normalized, [normalized, "1h", "1d"])


def _cached_market_rows(
    service: KlineService,
    *,
    market: str,
    symbol: str,
    timeframe: str,
    exchange_id: str,
    market_type: str,
    instrument_id: str,
) -> list[dict[str, Any]]:
    normalized_market = DataSourceFactory.normalize_market(market or "")
    exchange_keys = [str(exchange_id or "").strip().lower()]
    if exchange_keys[0]:
        exchange_keys.append("")
    market_type_keys = [str(market_type or "").strip().lower()]
    if market_type_keys[0]:
        market_type_keys.append("")
    instrument_keys = [str(instrument_id or "").strip()]
    if instrument_keys[0]:
        instrument_keys.append("")
    for ex_key in exchange_keys:
        for mt_key in market_type_keys:
            for native_key in instrument_keys:
                latest_key = (
                    f"kline:latest:{normalized_market}:{ex_key}:{mt_key}:{native_key}:"
                    f"{symbol}:{timeframe}"
                )
                cached = service.cache.get(latest_key)
                if cached:
                    return list(cached)
    for limit in (120, 300, 200, 100):
        for ex_key in exchange_keys:
            for mt_key in market_type_keys:
                for native_key in instrument_keys:
                    cache_key = (
                        f"kline:{normalized_market}:{ex_key}:{mt_key}:{native_key}:"
                        f"{symbol}:{timeframe}:{limit}"
                    )
                    cached = service.cache.get(cache_key)
                    if cached:
                        return list(cached)
    return []


def build_market_evidence(
    *,
    market: str,
    symbol: str,
    timeframe: str,
    exchange_id: str = "",
    market_type: str = "",
    instrument_id: str = "",
    reference_price: float = 0.0,
    primary_frame: Any = None,
    frame_bundle: Mapping[str, Any] | None = None,
    kline_service: KlineService | None = None,
) -> dict[str, Any]:
    service = kline_service or KlineService()
    timeframes = _decision_timeframes(timeframe)
    rows_by_timeframe: dict[str, list[dict[str, Any]]] = {}
    supplied_frames = dict(frame_bundle or {})
    supplied_frames.setdefault(timeframes[0], primary_frame)
    for target in timeframes:
        rows = _frame_rows(supplied_frames.get(target))
        if not rows:
            rows = _cached_market_rows(
                service,
                market=market,
                symbol=symbol,
                timeframe=target,
                exchange_id=exchange_id,
                market_type=market_type,
                instrument_id=instrument_id,
            )
        rows_by_timeframe[target] = rows

    summaries = {
        target: summarize_market_bars(
            rows_by_timeframe.get(target) or [],
            timeframe=target,
            reference_price=reference_price,
        )
        for target in timeframes
    }
    available = [target for target, item in summaries.items() if item.get("available")]
    fresh = [
        target
        for target, item in summaries.items()
        if item.get("available") and not item.get("is_stale")
    ]
    stale = [target for target in available if target not in fresh]
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "symbol": symbol,
        "exchange_id": exchange_id,
        "market_type": market_type,
        "primary_timeframe": timeframes[0],
        "requested_timeframes": timeframes,
        "available_timeframes": available,
        "fresh_timeframes": fresh,
        "stale_timeframes": stale,
        "data_quality": (
            "complete" if len(fresh) == len(timeframes)
            else "partial" if available
            else "unavailable"
        ),
        "timeframes": summaries,
    }


def _position_snapshot(positions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    gross_notional = 0.0
    unrealized_pnl = 0.0
    for position in positions or ():
        size = abs(float(_number(position.get("size"), 0.0) or 0.0))
        mark = float(_number(position.get("current_price"), 0.0) or 0.0)
        entry = float(_number(position.get("entry_price"), 0.0) or 0.0)
        side = str(position.get("side") or "").lower()
        direction = -1.0 if side == "short" else 1.0
        gross_notional += size * mark
        unrealized_pnl += (mark - entry) * size * direction if entry > 0 and mark > 0 else 0.0
        items.append({
            "symbol": position.get("symbol"),
            "side": side,
            "size": _rounded(size, 8),
            "entry_price": _rounded(entry),
            "current_price": _rounded(mark),
            "unrealized_pnl": _rounded((mark - entry) * size * direction, 4) if entry > 0 and mark > 0 else None,
        })
    return {
        "position_count": len(items),
        "gross_notional": _rounded(gross_notional, 4),
        "unrealized_pnl": _rounded(unrealized_pnl, 4),
        "positions": items[:20],
    }


def _strategy_parameters(trading_config: Any) -> dict[str, Any]:
    if not isinstance(trading_config, Mapping):
        return {}
    params = trading_config.get("params")
    if not isinstance(params, Mapping):
        return {}
    output: dict[str, Any] = {}
    for key, value in list(params.items())[:40]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            output[str(key)[:80]] = value if not isinstance(value, str) else value[:300]
    return output


def _strategy_performance(strategy_id: int) -> dict[str, Any]:
    if strategy_id <= 0:
        return {}
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE
                    THEN COALESCE(profit, 0) - COALESCE(commission_quote, commission, 0)
                    ELSE 0 END), 0) AS today_realized_pnl,
                  COALESCE(SUM(COALESCE(profit, 0) - COALESCE(commission_quote, commission, 0)), 0)
                    AS lifetime_realized_pnl,
                  COUNT(*) FILTER (WHERE type LIKE 'close_%%') AS completed_exits
                FROM qd_strategy_trades
                WHERE strategy_id = %s
                """,
                (strategy_id,),
            )
            row = cur.fetchone() or {}
            cur.execute(
                """
                SELECT COALESCE(profit, 0) - COALESCE(commission_quote, commission, 0) AS net_pnl
                FROM qd_strategy_trades
                WHERE strategy_id = %s AND type LIKE 'close_%%'
                ORDER BY id DESC
                LIMIT 10
                """,
                (strategy_id,),
            )
            recent = [float((item or {}).get("net_pnl") or 0) for item in (cur.fetchall() or [])]
            cur.close()
        consecutive_losses = 0
        for pnl in recent:
            if pnl >= 0:
                break
            consecutive_losses += 1
        return {
            "today_realized_pnl": _rounded(row.get("today_realized_pnl"), 4),
            "lifetime_realized_pnl": _rounded(row.get("lifetime_realized_pnl"), 4),
            "completed_exits": int(row.get("completed_exits") or 0),
            "recent_exit_pnl": [round(value, 4) for value in recent],
            "consecutive_losses": consecutive_losses,
        }
    except Exception as exc:
        logger.warning("AI decision strategy performance snapshot failed for %s: %s", strategy_id, exc)
        return {"available": False}


def build_strategy_decision_context(
    *,
    values: Mapping[str, Any],
    strategy: Mapping[str, Any],
    order_budget: Mapping[str, Any],
    strategy_equity: float,
    initial_capital: float,
    entry_percent: float,
) -> dict[str, Any]:
    symbol = str(values.get("symbol") or "")
    market = str(values.get("market_category") or "Crypto")
    market_type = str(values.get("market_type") or "spot")
    reference_price = float(values.get("current_price") or 0.0)
    timeframe = str(strategy.get("timeframe") or "15m")
    positions = values.get("current_positions") or ()
    trading_config = values.get("trading_config")
    position_state = _position_snapshot(positions)
    position_state.update({
        "strategy_equity": _rounded(strategy_equity, 4),
        "initial_capital": _rounded(initial_capital, 4),
        "drawdown_from_initial_pct": _rounded(
            ((strategy_equity / initial_capital) - 1.0) * 100.0 if initial_capital > 0 else None,
            3,
        ),
        "entry_percent": _rounded(entry_percent, 3),
        "gross_exposure_pct": _rounded(
            (float(position_state.get("gross_notional") or 0) / strategy_equity) * 100.0
            if strategy_equity > 0 else None,
            3,
        ),
    })
    return {
        "context_version": 2,
        "strategy": {
            "name": str(strategy.get("strategy_name") or ""),
            "timeframe": timeframe,
            "direction_mode": str((trading_config or {}).get("direction_mode") or "")
            if isinstance(trading_config, Mapping) else "",
            "parameters": _strategy_parameters(trading_config),
        },
        "market_evidence": build_market_evidence(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            exchange_id=str(values.get("price_exchange_id") or ""),
            market_type=market_type,
            instrument_id=str(values.get("price_instrument_id") or ""),
            reference_price=reference_price,
            primary_frame=values.get("market_frame"),
            frame_bundle=values.get("market_frames"),
        ),
        "portfolio_risk": position_state,
        "strategy_performance": _strategy_performance(int(values.get("strategy_id") or 0)),
        "protection": dict(values.get("protection") or {}),
        "order_budget": dict(order_budget),
    }


def build_quick_trade_decision_context(context: Mapping[str, Any]) -> dict[str, Any]:
    price = float(context.get("price") or 0.0)
    quantity = float(context.get("base_qty") or 0.0)
    if price <= 0 and quantity > 0:
        price = float(context.get("order_notional_usdt") or 0.0) / quantity
    tp_price = float(context.get("tp_price") or 0.0)
    sl_price = float(context.get("sl_price") or 0.0)
    action = "long" if str(context.get("side") or "").lower() == "buy" else "short"
    direction = 1.0 if action == "long" else -1.0
    risk = (price - sl_price) * direction if price > 0 and sl_price > 0 else 0.0
    reward = (tp_price - price) * direction if price > 0 and tp_price > 0 else 0.0
    balance = context.get("bal") if isinstance(context.get("bal"), dict) else {}
    return {
        "context_version": 2,
        "strategy": {"name": "quick_trade", "timeframe": "1m", "direction_mode": action},
        "market_evidence": build_market_evidence(
            market="Crypto",
            symbol=str(context.get("symbol") or ""),
            timeframe="1m",
            exchange_id=str(context.get("exchange_id") or ""),
            market_type=str(context.get("market_type") or "spot"),
            reference_price=price,
        ),
        "portfolio_risk": {
            "available_balance": _rounded(balance.get("available"), 4),
            "total_balance": _rounded(balance.get("total"), 4),
            "order_quote_amount": _rounded(context.get("usdt_amount"), 4),
            "risk_reward_ratio": _rounded(reward / risk, 3) if risk > 0 and reward > 0 else None,
        },
        "protection": {
            "take_profit_price": _rounded(tp_price),
            "stop_loss_price": _rounded(sl_price),
            "margin_mode": str(context.get("margin_mode") or ""),
        },
    }
