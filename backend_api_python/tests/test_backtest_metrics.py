import math

import pandas as pd
import pytest

from app.services.backtest.metrics import benchmark_level_curve, calculate_information_ratio


def _curve(returns, *, frequency="D"):
    value = 100.0
    index = pd.date_range("2026-01-01", periods=len(returns) + 1, freq=frequency, tz="UTC")
    points = [{"time": index[0].isoformat(), "value": value}]
    for timestamp, periodic_return in zip(index[1:], returns):
        value *= 1.0 + periodic_return
        points.append({"time": timestamp.isoformat(), "value": value})
    return points


def test_information_ratio_uses_aligned_periodic_returns_and_sample_tracking_error():
    result = calculate_information_ratio(
        _curve([0.01, 0.02, -0.01, 0.005]),
        _curve([0.005, 0.01, -0.005, 0.002]),
        benchmark="USStock:SPY",
        frequency="1d",
        annualization_factor=252,
        market="USStock",
    )

    assert result["status"] == "available"
    assert result["portfolioReturnAnnualized"] == pytest.approx(1.575)
    assert result["benchmarkReturnAnnualized"] == pytest.approx(0.756)
    assert result["informationRatio"] == pytest.approx(8.270196225621152)
    assert result["classification"] == "exceptional"
    assert result["observations"] == 4


def test_metric_samples_portfolio_at_native_benchmark_frequency():
    index = pd.date_range("2026-01-01", periods=61, freq="min", tz="UTC")
    values = [100.0 * (1.0001**position) for position in range(len(index))]
    portfolio = [
        {"time": timestamp.isoformat(), "value": value}
        for timestamp, value in zip(index, values)
    ]
    benchmark_frame = pd.DataFrame({"close": values[::15]}, index=index[::15])

    result = calculate_information_ratio(
        portfolio,
        benchmark_level_curve(benchmark_frame),
        benchmark="Crypto:BTC/USDT",
        frequency="15m",
        annualization_factor=365.25 * 24 * 4,
        market="Crypto",
    )

    assert result["status"] == "zero_tracking_error"
    assert result["observations"] == 4
    assert result["trackingErrorAnnualized"] == pytest.approx(0.0, abs=1e-12)


def test_metric_rejects_missing_continuous_market_periods():
    times = ["2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z", "2026-01-01T00:45:00Z"]
    portfolio = [{"time": time, "value": value} for time, value in zip(times, [100.0, 121.0, 133.1])]
    benchmark = [{"time": time, "value": value} for time, value in zip(times, [100.0, 102.01, 103.0301])]

    result = calculate_information_ratio(
        portfolio,
        benchmark,
        benchmark="Crypto:BTC/USDT",
        frequency="15m",
        annualization_factor=365.25 * 24 * 4,
        market="Crypto",
    )

    assert result["status"] == "insufficient_history"
    assert result["observations"] == 1


def test_metric_accepts_consecutive_equity_sessions_across_weekend():
    portfolio = [
        {"time": "2026-01-09T00:00:00Z", "value": 100.0},
        {"time": "2026-01-12T00:00:00Z", "value": 101.0},
    ]
    benchmark = [
        {"time": "2026-01-09T00:00:00Z", "value": 100.0},
        {"time": "2026-01-12T00:00:00Z", "value": 100.5},
    ]

    result = calculate_information_ratio(
        portfolio,
        benchmark,
        benchmark="USStock:SPY",
        frequency="1d",
        annualization_factor=252,
        market="USStock",
    )

    assert result["observations"] == 1
    assert result["portfolioReturnAnnualized"] == pytest.approx(2.52)


def test_information_ratio_preserves_negative_values_and_custom_bands():
    result = calculate_information_ratio(
        _curve([-0.01, -0.02, 0.005, -0.01]),
        _curve([0.005, -0.005, 0.01, 0.002]),
        benchmark="custom",
        frequency="1d",
        annualization_factor=252,
        market="USStock",
        classification_bands=((0.0, "negative"), (math.inf, "positive")),
    )

    assert result["informationRatio"] < 0
    assert result["classification"] == "negative"
