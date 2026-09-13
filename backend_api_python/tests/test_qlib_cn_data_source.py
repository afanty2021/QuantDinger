"""Unit tests for the offline qlib CN daily K-line tier (CNStock Tier 0).

Mini qlib roots are constructed in temporary directories with hand-written
calendars/instruments/bin files — no real qlib data, no network, no DB.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from app.data_sources import cn_stock, qlib_cn
from app.data_sources.cn_stock import CNStockDataSource
from app.data_sources.qlib_cn import (
    QlibBinStore,
    fetch_qlib_daily_klines,
    get_qlib_store,
    reset_qlib_store,
)
from app.data_sources.tencent import normalize_cn_code, parse_tencent_kline_time


# ---------------------------------------------------------------------------
# mini qlib root helpers
# ---------------------------------------------------------------------------

def _weekdays(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _write_calendar(root: str, days: list[date]) -> None:
    os.makedirs(os.path.join(root, "calendars"), exist_ok=True)
    with open(os.path.join(root, "calendars", "day.txt"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(d.isoformat() for d in days) + "\n")


def _write_instruments(root: str, lines: list[str]) -> None:
    os.makedirs(os.path.join(root, "instruments"), exist_ok=True)
    with open(os.path.join(root, "instruments", "all.txt"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


def _write_bin(root: str, code: str, field: str, values, start_index: int) -> None:
    path = os.path.join(root, "features", code, f"{field}.day.bin")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr = np.hstack(
        [np.array([start_index], dtype="<f4"), np.asarray(values, dtype="<f4")]
    )
    arr.tofile(path)


def _write_raw_bin(root: str, code: str, field: str, payload: bytes) -> None:
    path = os.path.join(root, "features", code, f"{field}.day.bin")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fp:
        fp.write(payload)


def _mini_root(tmp_path):
    """SH600519 with bins starting mid-calendar; SZ000001 with two listing
    intervals separated by a gap. One suspended (NaN) day inside SH600519."""
    root = str(tmp_path / "qlib")
    days = _weekdays(date(2024, 1, 2), date(2024, 3, 29))
    _write_calendar(root, days)
    _write_instruments(
        root,
        [
            "SH600519\t2024-01-02\t2024-03-29",
            "SZ000001\t2024-01-02\t2024-02-15",
            "SZ000001\t2024-02-20\t2024-03-29",
        ],
    )
    start_index = days.index(date(2024, 2, 1))
    n = len(days) - start_index
    idx = np.arange(n, dtype="<f4")
    close = 100.0 + idx
    suspended_day = days[start_index + 10]
    close[10] = np.nan  # suspended day -> whole OHLC row is NaN
    _write_bin(root, "sh600519", "open", close - 1.0, start_index)
    _write_bin(root, "sh600519", "close", close, start_index)
    _write_bin(root, "sh600519", "high", close + 1.0, start_index)
    _write_bin(root, "sh600519", "low", close - 2.0, start_index)
    _write_bin(root, "sh600519", "volume", 1000.0 + idx, start_index)
    _write_bin(root, "sh600519", "factor", np.ones(n, dtype="<f4"), start_index)

    m = len(days)
    idx2 = np.arange(m, dtype="<f4")
    close2 = 10.0 + idx2
    _write_bin(root, "sz000001", "open", close2 - 1.0, 0)
    _write_bin(root, "sz000001", "close", close2, 0)
    _write_bin(root, "sz000001", "high", close2 + 1.0, 0)
    _write_bin(root, "sz000001", "low", close2 - 2.0, 0)
    _write_bin(root, "sz000001", "volume", 500.0 + idx2, 0)
    _write_bin(root, "sz000001", "factor", np.ones(m, dtype="<f4"), 0)
    return {"root": root, "days": days, "start_index": start_index, "suspended_day": suspended_day}


def _year_end_root(tmp_path):
    """Calendar spanning the 2024/2025 ISO-year boundary."""
    root = str(tmp_path / "qlib_ye")
    days = [date(2024, 12, 27), date(2024, 12, 30), date(2024, 12, 31),
            date(2025, 1, 2), date(2025, 1, 3)]
    _write_calendar(root, days)
    _write_instruments(root, ["SH600519\t2024-12-27\t2025-01-03"])
    close = np.array([10.0, 11.0, 12.0, 13.0, 14.0], dtype="<f4")
    vol = np.array([100.0, 200.0, 300.0, 400.0, 500.0], dtype="<f4")
    _write_bin(root, "sh600519", "open", close - 1.0, 0)
    _write_bin(root, "sh600519", "close", close, 0)
    _write_bin(root, "sh600519", "high", close + 2.0, 0)
    _write_bin(root, "sh600519", "low", close - 2.0, 0)
    _write_bin(root, "sh600519", "volume", vol, 0)
    _write_bin(root, "sh600519", "factor", np.ones(5, dtype="<f4"), 0)
    return {"root": root, "days": days}


# ---------------------------------------------------------------------------
# env / freshness helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def qlib_env(monkeypatch):
    monkeypatch.delenv("QLIB_CN_DATA_DIR", raising=False)
    monkeypatch.delenv("QLIB_CN_LENIENT", raising=False)
    monkeypatch.delenv("QLIB_CN_STALENESS_DAYS", raising=False)
    qlib_cn._warn_days.clear()
    reset_qlib_store()
    yield
    reset_qlib_store()


def _enable(monkeypatch, root: str) -> None:
    monkeypatch.setenv("QLIB_CN_DATA_DIR", root)
    reset_qlib_store()


def _set_fresh(monkeypatch, epoch) -> None:
    """Pin the 'last completed A-share session' used by strict freshness."""
    monkeypatch.setattr(qlib_cn, "_last_completed_session_epoch", lambda: epoch)


def _mock_online_tiers(monkeypatch):
    """Stub every online fetcher used by CNStockDataSource; returns call log."""
    calls = []

    def _td(**kwargs):
        calls.append("twelvedata")
        return []

    def _yf(**kwargs):
        calls.append("yfinance")
        return []

    def _ak_minute(**kwargs):
        calls.append("akshare-minute")
        return []

    def _ak_weekly(**kwargs):
        calls.append("akshare-weekly")
        return []

    def _tencent(code, period="day", count=300, adj="qfq", timeout=10):
        calls.append(f"tencent:{period}")
        return [["2024-03-28", "101", "102", "103", "100", "500"]]

    monkeypatch.setattr(cn_stock, "fetch_twelvedata_klines", _td)
    monkeypatch.setattr(cn_stock, "fetch_yfinance_klines", _yf)
    monkeypatch.setattr(cn_stock, "fetch_akshare_minute_klines", _ak_minute)
    monkeypatch.setattr(cn_stock, "fetch_akshare_weekly_klines", _ak_weekly)
    monkeypatch.setattr(cn_stock, "fetch_kline", _tencent)
    return calls


def _epoch(day: date) -> int:
    return parse_tencent_kline_time(day.isoformat())


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_disabled_without_env_zero_behavior_change(qlib_env, monkeypatch):
    assert get_qlib_store() is None
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    calls = _mock_online_tiers(monkeypatch)
    rows = CNStockDataSource().get_kline("600519", "1D", 5)
    assert rows and rows[0]["close"] == 102.0
    assert calls == ["twelvedata", "tencent:day"]


def test_basic_daily_window_and_values(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    rows = fetch_qlib_daily_klines("sh600519", "1D", 10)
    assert rows is not None and len(rows) == 10
    days = info["days"]
    last = days[-1]
    assert rows[-1]["time"] == _epoch(last)
    assert rows[-1]["close"] == pytest.approx(100.0 + (len(info["days"]) - 1 - info["start_index"]))
    assert rows[0]["time"] == _epoch(days[-10])
    # limit=2 (the get_realtime_price fallback shape) returns the last 2 bars
    rows2 = fetch_qlib_daily_klines("sh600519", "1D", 2)
    assert [r["time"] for r in rows2] == [_epoch(days[-2]), _epoch(days[-1])]


def test_bin_header_alignment_nonzero_start(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    first_bin_day = info["days"][info["start_index"]]
    rows = fetch_qlib_daily_klines("sh600519", "1D", 10000, after_time=_epoch(first_bin_day))
    assert rows is not None
    assert rows[0]["time"] == _epoch(first_bin_day)
    expected = len(info["days"]) - info["start_index"] - 1  # minus suspended day
    assert len(rows) == expected


def test_limit_before_after_semantics(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    days = info["days"]

    # before_time is exclusive (time < before_time)
    pivot = days[40]
    rows = fetch_qlib_daily_klines("sh600519", "1D", 5, before_time=_epoch(pivot))
    assert rows[-1]["time"] == _epoch(days[39])
    assert all(r["time"] < _epoch(pivot) for r in rows)

    # after_time + before_time: full window despite small limit (truncate=False)
    a, b = days[30], days[50]
    rows = fetch_qlib_daily_klines(
        "sh600519", "1D", 5, before_time=_epoch(b), after_time=_epoch(a)
    )
    expected = [d for d in days if a <= d < b]
    dropped = info["suspended_day"]
    expected = [d for d in expected if d != dropped]
    assert [datetime.fromtimestamp(r["time"]).date() for r in rows] == expected

    # window starting before the bin data starts (left-edge hole) falls through
    early = info["days"][5]
    rows = fetch_qlib_daily_klines(
        "sh600519", "1D", 5, before_time=_epoch(days[15]), after_time=_epoch(early)
    )
    assert rows is None


def test_suspended_nan_day_dropped(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    dropped = info["suspended_day"]
    rows = fetch_qlib_daily_klines("sh600519", "1D", len(info["days"]), after_time=_epoch(info["days"][info["start_index"]]))
    times = {datetime.fromtimestamp(r["time"]).date() for r in rows}
    assert dropped not in times
    assert len(rows) == len(info["days"]) - info["start_index"] - 1


def test_symbol_normalization_equivalence(qlib_env, monkeypatch, tmp_path):
    assert normalize_cn_code("600519") == normalize_cn_code("600519.SH") == "SH600519"
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    a = fetch_qlib_daily_klines("sh600519", "1D", 5)
    b = fetch_qlib_daily_klines("SH600519", "1D", 5)
    assert a == b


def test_instruments_interval_gate(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    # window entirely inside the listing gap between SZ000001 intervals
    gap_start, gap_end = date(2024, 2, 16), date(2024, 2, 19)
    rows = fetch_qlib_daily_klines(
        "sz000001", "1D", 5, before_time=_epoch(gap_end) + 1, after_time=_epoch(gap_start)
    )
    assert rows is None

    # window overlapping a listed interval works
    rows = fetch_qlib_daily_klines(
        "sz000001", "1D", 5, before_time=_epoch(date(2024, 3, 1)) + 1,
        after_time=_epoch(date(2024, 2, 26)),
    )
    assert rows and len(rows) == 5

    # unknown symbol falls through
    assert fetch_qlib_daily_klines("sz600000", "1D", 5) is None


def test_missing_or_invalid_sources_fall_through(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    # missing bin file
    os.remove(os.path.join(info["root"], "features", "sz000001", "close.day.bin"))
    assert fetch_qlib_daily_klines("sz000001", "1D", 5) is None

    # invalid directory disables the tier entirely
    monkeypatch.setenv("QLIB_CN_DATA_DIR", str(tmp_path / "does-not-exist"))
    reset_qlib_store()
    assert get_qlib_store() is None
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00" * 4,   # header only (no values)
        b"\x00" * 6,   # size not a multiple of 4
        bytes(np.hstack([np.array([1e6], dtype="<f4"), np.array([1.0], dtype="<f4")]).tobytes()),  # header beyond calendar
        bytes(np.hstack([np.array([-5.0], dtype="<f4"), np.array([1.0], dtype="<f4")]).tobytes()),  # negative header
    ],
)
def test_corrupt_bins_return_none(qlib_env, monkeypatch, tmp_path, payload):
    info = _mini_root(tmp_path)
    _write_raw_bin(info["root"], "sz000001", "close", payload)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    assert fetch_qlib_daily_klines("sz000001", "1D", 5) is None


def test_strict_freshness_stale_tail_vs_lenient(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    # "today" data is newer than the calendar end -> stale tail
    _set_fresh(monkeypatch, parse_tencent_kline_time("2024-04-01"))
    calls = _mock_online_tiers(monkeypatch)

    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    rows = CNStockDataSource().get_kline("600519", "1D", 5)
    assert rows and rows[0]["close"] == 102.0
    assert "twelvedata" in calls

    # lenient mode explicitly allows the stale tail
    monkeypatch.setenv("QLIB_CN_LENIENT", "1")
    served = fetch_qlib_daily_klines("sh600519", "1D", 5)
    assert served is not None and len(served) == 5

    # strict mode serves again once the calendar covers the completed session
    monkeypatch.delenv("QLIB_CN_LENIENT")
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is not None


def test_cn_stock_serves_from_qlib_before_online_tiers(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    calls = _mock_online_tiers(monkeypatch)

    rows = CNStockDataSource().get_kline("600519", "1D", 5)
    assert len(rows) == 5
    assert calls == []  # no online fetcher touched
    days = info["days"]
    assert rows[-1]["time"] == _epoch(days[-1])

    # unsupported timeframe still goes online
    rows = CNStockDataSource().get_kline("600519", "5m", 5)
    assert rows == []
    assert calls == ["twelvedata", "yfinance", "akshare-minute"]


def test_weekly_aggregation_cross_year_calendar_week(qlib_env, monkeypatch, tmp_path):
    info = _year_end_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    # Calendar-week buckets (Monday-based), matching the Tencent weekly tier:
    # the ISO new-year week 12/30..01/03 stays ONE bar labeled by its last day.
    rows = fetch_qlib_daily_klines("sh600519", "1W", 10)
    assert rows is not None and len(rows) == 2
    first, second = rows
    assert first["time"] == _epoch(date(2024, 12, 27))  # solo Friday bar
    assert first["open"] == pytest.approx(9.0)
    assert first["high"] == pytest.approx(12.0)
    assert first["low"] == pytest.approx(8.0)
    assert first["close"] == pytest.approx(10.0)
    assert first["volume"] == pytest.approx(100.0)
    assert second["time"] == _epoch(date(2025, 1, 3))  # 12/30..01/03 in one bucket
    assert second["open"] == pytest.approx(10.0)
    assert second["high"] == pytest.approx(16.0)
    assert second["low"] == pytest.approx(9.0)
    assert second["close"] == pytest.approx(14.0)
    assert second["volume"] == pytest.approx(1400.0)

    # limit counts weekly bars, not daily ones: the trim to `limit` happens in
    # CNStockDataSource.filter_and_limit on top of the aggregated buckets
    rows = CNStockDataSource().get_kline("600519", "1W", 1)
    assert len(rows) == 1 and rows[0]["time"] == _epoch(date(2025, 1, 3))


def test_timestamps_match_tencent_convention(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    rows = fetch_qlib_daily_klines("sh600519", "1D", 5)
    for row in rows:
        day = datetime.fromtimestamp(row["time"]).date().isoformat()
        assert row["time"] == parse_tencent_kline_time(day)


def test_stale_calendar_warns_once_and_still_serves_in_lenient(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    monkeypatch.setenv("QLIB_CN_LENIENT", "1")
    assert qlib_cn.QlibCNConfig.STALENESS_DAYS == 7

    rows = fetch_qlib_daily_klines("sh600519", "1D", 5)
    assert rows is not None
    assert qlib_cn._warn_days.get("stale") == date.today()

    # second fetch the same day does not re-warn (limited to once per day)
    fetch_qlib_daily_klines("sh600519", "1D", 5)
    warned = [key for key in qlib_cn._warn_days if key == "stale"]
    assert warned == ["stale"]


def test_store_singleton_and_reset(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    monkeypatch.setenv("QLIB_CN_DATA_DIR", info["root"])
    reset_qlib_store()
    store_a = get_qlib_store()
    assert isinstance(store_a, QlibBinStore)
    assert get_qlib_store() is store_a  # cached singleton
    reset_qlib_store()
    assert get_qlib_store() is not store_a


def test_store_rejects_path_traversal(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    store = get_qlib_store()
    assert store.read_field("../../etc", "close") is None
    assert store.read_field("sh600519", "close;rm") is None
