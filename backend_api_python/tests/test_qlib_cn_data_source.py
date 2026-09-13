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
    monkeypatch.delenv("QLIB_CN_EXPOSE_STORED", raising=False)
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


def _append_bin_value(root: str, code: str, field: str, value: float) -> None:
    """Simulate an incremental updater's append to an existing field bin."""
    path = os.path.join(root, "features", code, f"{field}.day.bin")
    arr = np.fromfile(path, dtype="<f4")
    np.hstack([arr, np.array([value], dtype="<f4")]).tofile(path)


def test_appended_sessions_visible_without_restart(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    # Warm every cache (calendar, instruments, field LRU) with a first fetch.
    assert fetch_qlib_daily_klines("sh600519", "1D", 3) is not None

    # Incremental update while the process keeps running: one appended session
    # (calendar + instrument bins) plus a brand-new symbol in all.txt.
    new_day = date(2024, 4, 1)
    new_index = len(info["days"])
    _write_calendar(info["root"], info["days"] + [new_day])
    _write_instruments(
        info["root"],
        [
            "SH600519\t2024-01-02\t2024-04-01",
            "SZ000001\t2024-01-02\t2024-02-15",
            "SZ000001\t2024-02-20\t2024-03-29",
            "SH600000\t2024-04-01\t2024-04-01",
        ],
    )
    for field, value in (
        ("open", 199.0), ("high", 201.0), ("low", 198.0),
        ("close", 200.0), ("volume", 2000.0), ("factor", 1.0),
    ):
        _append_bin_value(info["root"], "sh600519", field, value)
    _write_bin(info["root"], "sh600000", "open", [9.0], new_index)
    _write_bin(info["root"], "sh600000", "close", [10.0], new_index)
    _write_bin(info["root"], "sh600000", "high", [11.0], new_index)
    _write_bin(info["root"], "sh600000", "low", [8.0], new_index)
    _write_bin(info["root"], "sh600000", "volume", [500.0], new_index)
    _write_bin(info["root"], "sh600000", "factor", [1.0], new_index)

    # Strict mode now requires the appended session; it must be served live.
    _set_fresh(monkeypatch, _epoch(new_day))
    rows = fetch_qlib_daily_klines("sh600519", "1D", 3)
    assert rows is not None and rows[-1]["time"] == _epoch(new_day)
    assert rows[-1]["close"] == pytest.approx(200.0)

    # The newly listed symbol becomes visible without a restart too.
    rows = fetch_qlib_daily_klines("sh600000", "1D", 3, after_time=_epoch(new_day))
    assert rows is not None and [r["time"] for r in rows] == [_epoch(new_day)]
    assert rows[0]["close"] == pytest.approx(10.0)


def test_strict_mode_serves_historical_window_despite_stale_calendar(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    # Calendar is a month stale: touch-now windows must fall through...
    _set_fresh(monkeypatch, parse_tencent_kline_time("2024-04-10"))
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    # ...but a historical before_time window only requires coverage up to
    # min(before_time, last_completed), which this calendar satisfies.
    pivot = info["days"][45]
    rows = fetch_qlib_daily_klines("sh600519", "1D", 5, before_time=_epoch(pivot))
    assert rows is not None and len(rows) == 5
    assert rows[-1]["time"] == _epoch(info["days"][44])
    assert all(r["time"] < _epoch(pivot) for r in rows)


def test_right_edge_short_bin_falls_through(qlib_env, monkeypatch, tmp_path):
    info = _mini_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    # One field truncated a session short of the calendar end: a right-edge
    # hole must fall through with a precise warning, not raise or serve short.
    path = os.path.join(info["root"], "features", "sh600519", "volume.day.bin")
    arr = np.fromfile(path, dtype="<f4")
    arr[:-1].tofile(path)
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    assert qlib_cn._warn_days.get("edge") == date.today()


# ---------------------------------------------------------------------------
# factor != 1: tail-anchored qfq conversion (design 20260913 §5)
# ---------------------------------------------------------------------------

def _factor_root(tmp_path, suspend: bool = False):
    """SH600519 with a real adjustment factor: 0.5 for the first ten sessions
    and 0.505 afterwards (ex-div jump at the midpoint). Bins store the
    investment_data contract: stored_price = raw * f, stored_volume = raw/f."""
    root = str(tmp_path / "qlib_f")
    days = _weekdays(date(2024, 1, 2), date(2024, 1, 29))
    _write_calendar(root, days)
    _write_instruments(root, [f"SH600519\t{days[0].isoformat()}\t{days[-1].isoformat()}"])
    n = len(days)
    f = np.array([0.5] * 10 + [0.505] * (n - 10), dtype="<f4")
    raw_close = 100.0 + np.arange(n, dtype="<f4")
    raw_vol = 1000.0 + np.arange(n, dtype="<f4")
    stored_open = (raw_close - 1.0) * f
    stored_close = raw_close * f
    stored_high = (raw_close + 1.0) * f
    stored_low = (raw_close - 2.0) * f
    if suspend:
        # Suspension row: OHLC (and factor, as generic dumps do) become NaN.
        stored_open[5] = np.nan
        stored_close[5] = np.nan
        stored_high[5] = np.nan
        stored_low[5] = np.nan
        f[5] = np.nan
    _write_bin(root, "sh600519", "open", stored_open, 0)
    _write_bin(root, "sh600519", "close", stored_close, 0)
    _write_bin(root, "sh600519", "high", stored_high, 0)
    _write_bin(root, "sh600519", "low", stored_low, 0)
    _write_bin(root, "sh600519", "volume", raw_vol / f, 0)
    _write_bin(root, "sh600519", "factor", f, 0)
    return {"root": root, "days": days, "f": f, "raw_close": raw_close, "raw_vol": raw_vol}


def _fetch_all(info):
    return fetch_qlib_daily_klines(
        "sh600519", "1D", len(info["days"]) + 10, after_time=_epoch(info["days"][0])
    )


def _reload_bins():
    """Simulate a process restart for the field LRU: rewritten bins are only
    picked up after a restart by design (live pickup covers appends, which
    bump the calendar stamp and clear the cache; content rewrites do not)."""
    store = get_qlib_store()
    if store is not None:
        store._read_field_cached.cache_clear()


def test_factor_rebase_ohlc_and_volume(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    f, raw_close, raw_vol, days = info["f"], info["raw_close"], info["raw_vol"], info["days"]
    f_last = float(f[-1])

    rows = _fetch_all(info)
    assert len(rows) == len(days)  # no suspension row in this fixture
    for row, i in zip(rows, range(len(days))):
        # OHLC divided by f_last: raw * f[t] / f_last.
        assert row["close"] == pytest.approx(float(raw_close[i]) * float(f[i]) / f_last, abs=1e-4)
        assert row["open"] == pytest.approx((float(raw_close[i]) - 1.0) * float(f[i]) / f_last, abs=1e-4)
        # Volume restored to raw: stored * f[t].
        assert row["volume"] == pytest.approx(float(raw_vol[i]), abs=0.05)


def test_factor_weekly_volume_summed_after_restoration(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    f, raw_vol, days = info["f"], info["raw_vol"], info["days"]

    rows = fetch_qlib_daily_klines("sh600519", "1W", 10)
    assert rows is not None
    # The ex-div jump (index 9 -> 10) sits inside one calendar week: verify the
    # weekly volume equals the sum of per-day RESTORED volumes (not stored).
    ex_div_week = [9, 10, 11, 12, 13]
    expected = round(sum(round(float(raw_vol[j]), 2) for j in ex_div_week), 2)
    bucket = next(r for r in rows if r["time"] == _epoch(days[13]))
    assert bucket["volume"] == pytest.approx(expected, abs=0.05)


def test_latest_row_equals_raw(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    rows = _fetch_all(info)
    # Tail anchor: the newest exposed close IS the raw price.
    assert rows[-1]["close"] == pytest.approx(float(info["raw_close"][-1]), abs=1e-4)
    assert rows[-1]["open"] == pytest.approx(float(info["raw_close"][-1]) - 1.0, abs=1e-4)


def test_return_preserved_across_ex_div_jump(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    f, raw_close = info["f"], info["raw_close"]

    rows = _fetch_all(info)
    emitted = list(range(len(info["days"])))
    exposed = [r["close"] for r in rows]
    assert len(exposed) == len(emitted)
    for k in range(1, len(emitted)):
        i_prev, i_curr = emitted[k - 1], emitted[k]
        stored_ratio = float(raw_close[i_curr] * f[i_curr]) / float(raw_close[i_prev] * f[i_prev])
        # Rounded independently at different magnitudes: rel 1e-5, not bitwise.
        assert exposed[k] / exposed[k - 1] == pytest.approx(stored_ratio, rel=1e-5)
    # The ex-div jump itself (index 9 -> 10) is covered by the loop above.


def test_factor_corruption_falls_through(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    factor_path = os.path.join(info["root"], "features", "sh600519", "factor.day.bin")
    arr = np.fromfile(factor_path, dtype="<f4")

    def rewrite(values):
        np.asarray(values, dtype="<f4").tofile(factor_path)
        _reload_bins()

    # Missing factor bin -> no conversion possible.
    os.remove(factor_path)
    _reload_bins()
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    rewrite(arr)  # restore

    # Unusable tail factor: 0 / negative / +inf / NaN.
    for bad_tail in (0.0, -0.5, np.inf, np.nan):
        bad = arr.copy()
        bad[-1] = bad_tail
        rewrite(bad)
        assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    rewrite(arr)  # restore

    # Non-finite / non-positive factor on a row OUTSIDE the served window
    # (limit=5 serves indices 15..19): must NOT affect the response.
    for odd_mid in (np.nan, -0.5):
        bad = arr.copy()
        bad[7] = odd_mid
        rewrite(bad)
        assert fetch_qlib_daily_klines("sh600519", "1D", 5) is not None
    rewrite(arr)  # restore

    # Non-finite / non-positive factor ON A SERVED row (index 16): the
    # conversion would silently produce zero/negative volume -- fall through.
    for bad_mid in (np.nan, -0.5):
        bad = arr.copy()
        bad[16] = bad_mid
        rewrite(bad)
        assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    rewrite(arr)  # restore
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is not None


def test_suspension_row_with_nan_factor_still_served(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path, suspend=True)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))

    # Generic dumps fill suspension rows with NaN (factor included); the row
    # is dropped anyway, so the window must still be served.
    rows = _fetch_all(info)
    assert rows is not None
    assert len(rows) == len(info["days"]) - 1
    assert all(r["time"] != _epoch(info["days"][5]) for r in rows)


def test_torn_generation_factor_close_mismatch_falls_through(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    factor_path = os.path.join(info["root"], "features", "sh600519", "factor.day.bin")
    close_path = os.path.join(info["root"], "features", "sh600519", "close.day.bin")
    arr = np.fromfile(factor_path, dtype="<f4")

    # A factor bin longer than the calendar is rejected outright by the bin
    # header validation (still a fall-through -- never served mis-based).
    np.hstack([arr, np.array([0.505], dtype="<f4")]).tofile(factor_path)
    _reload_bins()
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    arr.tofile(factor_path)
    _reload_bins()

    # Both bins individually valid but from different generations: close
    # starting one session later than factor -> (start, size) mismatch.
    close_arr = np.fromfile(close_path, dtype="<f4")
    torn = np.hstack([np.array([1.0], dtype="<f4"), close_arr[1:-1]])
    torn.tofile(close_path)
    _reload_bins()
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    assert qlib_cn._warn_days.get("generation") == date.today()
    close_arr.tofile(close_path)
    _reload_bins()

    # Shorter factor bin with a window that does NOT reach the calendar tail:
    # still a generation mismatch (anchor would sit at an earlier row).
    arr[:-1].tofile(factor_path)
    _reload_bins()
    qlib_cn._warn_days.clear()
    pivot = _epoch(info["days"][15]) + 1
    rows = fetch_qlib_daily_klines("sh600519", "1D", 5, before_time=pivot)
    assert rows is None
    assert qlib_cn._warn_days.get("generation") == date.today()


def test_short_factor_bin_right_edge_warns(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    factor_path = os.path.join(info["root"], "features", "sh600519", "factor.day.bin")
    arr = np.fromfile(factor_path, dtype="<f4")
    # A factor array ending before the window's last day must trip the precise
    # "edge" gate (inherited by joining the fields dict), not a generic error.
    arr[:-1].tofile(factor_path)
    assert fetch_qlib_daily_klines("sh600519", "1D", 5) is None
    assert qlib_cn._warn_days.get("edge") == date.today()


def test_single_row_window_with_factor(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    last_epoch = _epoch(info["days"][-1])

    rows = fetch_qlib_daily_klines("sh600519", "1D", 1, after_time=last_epoch)
    assert rows is not None and len(rows) == 1
    assert rows[0]["close"] == pytest.approx(float(info["raw_close"][-1]), abs=1e-4)


def test_expose_stored_escape_hatch(qlib_env, monkeypatch, tmp_path):
    info = _factor_root(tmp_path)
    _enable(monkeypatch, info["root"])
    _set_fresh(monkeypatch, _epoch(info["days"][-1]))
    f = info["f"]
    raw_close, raw_vol = info["raw_close"], info["raw_vol"]

    monkeypatch.setenv("QLIB_CN_EXPOSE_STORED", "1")
    rows = _fetch_all(info)
    assert len(rows) == len(info["days"])
    for row, i in zip(rows, range(len(info["days"]))):
        # Identity passthrough: stored values as-is (20260912 behavior).
        assert row["close"] == pytest.approx(float(raw_close[i]) * float(f[i]), abs=1e-4)
        assert row["volume"] == pytest.approx(float(raw_vol[i]) / float(f[i]), abs=0.05)
    assert qlib_cn._warn_days.get("expose-stored") == date.today()

    # The hatch also serves roots that have no factor bins at all.
    os.remove(os.path.join(info["root"], "features", "sh600519", "factor.day.bin"))
    rows = _fetch_all(info)
    assert rows is not None and len(rows) == len(info["days"])
    assert rows[-1]["close"] == pytest.approx(float(raw_close[-1]) * float(f[-1]), abs=1e-4)
