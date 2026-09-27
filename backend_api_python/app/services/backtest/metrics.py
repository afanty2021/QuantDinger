"""Benchmark-relative performance metrics for backtest result curves."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import pandas as pd

from app.services.market_schedule import CALENDAR_BY_MARKET, equity_bar_session_date


_FREQUENCY_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14_400,
    "1d": 86_400,
    "1w": 604_800,
}


DEFAULT_INFORMATION_RATIO_BANDS: tuple[tuple[float, str], ...] = (
    (0.30, "weak"),
    (0.50, "acceptable"),
    (1.00, "good"),
    (math.inf, "exceptional"),
)


def calculate_information_ratio(
    portfolio_curve: Iterable[dict[str, Any]],
    benchmark_curve: Iterable[dict[str, Any]],
    *,
    benchmark: str | None,
    frequency: str,
    annualization_factor: float,
    market: str = "",
    classification_bands: Sequence[tuple[float, str]] = DEFAULT_INFORMATION_RATIO_BANDS,
) -> dict[str, Any]:
    """Calculate an Information Ratio from levels observed at common timestamps.

    The portfolio is sampled only at native benchmark observations before
    returns are calculated. This keeps every active return on one common
    interval when the benchmark is intentionally fetched at a coarser
    frequency than the strategy. Gaps that are not valid consecutive market
    periods are excluded instead of being annualized as a single period.
    """
    normalized_frequency = _normalize_frequency(frequency)
    factor = float(annualization_factor)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("annualization_factor must be a positive finite number")

    bands = _validate_classification_bands(classification_bands)
    levels = pd.concat(
        (
            _curve_levels(portfolio_curve).rename("portfolio"),
            _curve_levels(benchmark_curve).rename("benchmark"),
        ),
        axis=1,
        join="inner",
    ).dropna()
    returns = levels.pct_change(fill_method=None).dropna()
    if not returns.empty:
        valid_intervals = _valid_interval_mask(levels.index, normalized_frequency, market)
        returns = returns.loc[valid_intervals]
        returns = returns.loc[
            returns.apply(lambda row: all(math.isfinite(float(value)) for value in row), axis=1)
        ]

    observations = int(len(returns.index))
    result = {
        "status": "insufficient_history",
        "portfolioReturnAnnualized": None,
        "benchmarkReturnAnnualized": None,
        "activeReturnAnnualized": None,
        "trackingErrorAnnualized": None,
        "informationRatio": None,
        "classification": None,
        "benchmark": benchmark,
        "observations": observations,
        "frequency": normalized_frequency,
        "annualizationFactor": factor,
    }
    if observations == 0:
        return result

    active_returns = returns["portfolio"] - returns["benchmark"]
    portfolio_annualized = float(returns["portfolio"].mean()) * factor
    benchmark_annualized = float(returns["benchmark"].mean()) * factor
    active_annualized = float(active_returns.mean()) * factor
    result.update({
        "portfolioReturnAnnualized": portfolio_annualized,
        "benchmarkReturnAnnualized": benchmark_annualized,
        "activeReturnAnnualized": active_annualized,
    })
    if observations < 2:
        return result

    periodic_tracking_error = float(active_returns.std(ddof=1))
    tracking_error_annualized = periodic_tracking_error * math.sqrt(factor)
    result["trackingErrorAnnualized"] = tracking_error_annualized
    if math.isclose(periodic_tracking_error, 0.0, rel_tol=0.0, abs_tol=1e-15):
        result["status"] = "zero_tracking_error"
        return result

    information_ratio = active_annualized / tracking_error_annualized
    result.update({
        "status": "available",
        "informationRatio": information_ratio,
        "classification": _classify_information_ratio(information_ratio, bands),
    })
    return result


def benchmark_level_curve(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    """Return valid native-frequency benchmark levels without forward filling."""
    if frame is None or frame.empty or "close" not in frame.columns:
        return []
    values = pd.to_numeric(frame["close"], errors="coerce")
    index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="coerce", utc=True))
    points: list[dict[str, Any]] = []
    for timestamp, value in zip(index, values):
        if pd.isna(timestamp) or pd.isna(value):
            continue
        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value <= 0:
            continue
        points.append({"time": timestamp.isoformat().replace("+00:00", "Z"), "value": numeric_value})
    return points


def _curve_levels(curve: Iterable[dict[str, Any]]) -> pd.Series:
    rows = []
    for point in curve:
        timestamp = pd.to_datetime(point.get("time"), errors="coerce", utc=True)
        value = pd.to_numeric(point.get("value"), errors="coerce")
        if pd.isna(timestamp) or pd.isna(value):
            continue
        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value <= 0:
            continue
        rows.append((timestamp, numeric_value))
    if not rows:
        return pd.Series(dtype="float64")
    levels = pd.Series(
        (value for _, value in rows),
        index=pd.DatetimeIndex(timestamp for timestamp, _ in rows),
        dtype="float64",
    )
    return levels.loc[~levels.index.duplicated(keep="last")].sort_index()


def _valid_interval_mask(index: pd.DatetimeIndex, frequency: str, market: str) -> pd.Series:
    if len(index) < 2:
        return pd.Series(dtype="bool")
    expected = pd.Timedelta(seconds=_FREQUENCY_SECONDS.get(frequency, 86_400))
    valid: list[bool] = []
    for start, end in zip(index[:-1], index[1:]):
        delta = end - start
        if delta == expected:
            valid.append(True)
            continue
        if str(market or "").strip() in {"Crypto", "Cryptocurrency"}:
            valid.append(False)
            continue
        if frequency == "1w":
            valid.append(pd.Timedelta(days=4) <= delta <= pd.Timedelta(days=10))
            continue
        valid.append(_are_consecutive_equity_sessions(start, end, market))
    return pd.Series(valid, index=index[1:], dtype="bool")


def _are_consecutive_equity_sessions(start: pd.Timestamp, end: pd.Timestamp, market: str) -> bool:
    calendar_code = CALENDAR_BY_MARKET.get(str(market or "").strip())
    if not calendar_code:
        return False
    start_date = equity_bar_session_date(start, market)
    end_date = equity_bar_session_date(end, market)
    if start_date == end_date:
        return False
    try:
        import exchange_calendars

        calendar = exchange_calendars.get_calendar(calendar_code)
        sessions = calendar.sessions_in_range(pd.Timestamp(start_date), pd.Timestamp(end_date))
    except (ImportError, ValueError):
        return False
    if len(sessions) != 2:
        return False
    return sessions[0].date() == start_date and sessions[1].date() == end_date


def _normalize_frequency(value: object) -> str:
    normalized = str(value or "1d").strip().lower()
    aliases = {"daily": "1d", "day": "1d", "d": "1d", "weekly": "1w", "week": "1w", "w": "1w"}
    return aliases.get(normalized, normalized)


def _validate_classification_bands(
    classification_bands: Sequence[tuple[float, str]],
) -> tuple[tuple[float, str], ...]:
    bands = tuple((float(limit), str(label).strip()) for limit, label in classification_bands)
    if not bands or any(not label or math.isnan(limit) for limit, label in bands):
        raise ValueError("classification_bands must contain labelled upper bounds")
    if any(current <= previous for (previous, _), (current, _) in zip(bands, bands[1:])):
        raise ValueError("classification band upper bounds must be strictly increasing")
    return bands


def _classify_information_ratio(
    value: float,
    bands: Sequence[tuple[float, str]],
) -> str:
    for upper_bound, label in bands:
        if value < upper_bound:
            return label
    return bands[-1][1]
