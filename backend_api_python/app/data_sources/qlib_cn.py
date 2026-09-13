"""
Offline qlib CN daily K-line tier (Tier 0) for CNStockDataSource.

Reads qlib's binary dump format directly -- little-endian float32 arrays whose
first element is the start index into ``calendars/day.txt`` -- without importing
qlib. Enabled only when ``QLIB_CN_DATA_DIR`` points at a valid qlib root;
every failure inside this tier degrades to ``None`` so the online fallback
chain in ``CNStockDataSource`` stays intact.

Storage semantics (verified against qlib's dump pipeline, re-measured
2026-09-13): stored prices are adjusted (``raw * factor``), stored volume is
adjusted (``raw / factor``). The investment_data convention is a normalized
*cumulative* adjustment -- factor stays well below 1 for the whole series
(measured ~0.24 for SH600519, jumping at ex-dividend dates), which is NOT the
broker-style qfq that anchors at the latest session. Rows are therefore
exposed re-based to the tail factor (``stored / f_last``): the latest row
equals the raw price, magnitudes match the online tiers, and ex-dividend
continuity is preserved. Volume is restored to raw (``stored * factor[t]``)
to match the online tiers. Factor integrity gates (tail sanity, same
generation as the close bin, finite/positive on served rows) degrade the
whole tier to a fall-through instead of serving mis-based data. Set
``QLIB_CN_EXPOSE_STORED=1`` to serve stored values unchanged (no factor
bins required; adjustment basis unverified).

Windows are served all-or-nothing: when the qlib calendar does not fully
cover the requested window (including the tail up to the last completed
A-share session, in the default strict freshness mode) this tier returns
``None`` and never stitches two adjustment bases into one series.

Update concurrency: the qlib root is treated as append-only. Reads use
``np.fromfile`` (never mmap) with a 4-byte size check so a torn append degrades
to a fall-through instead of crashing the process. Appended sessions become
visible without a restart: calendar and instrument files are re-read when their
stamp (mtime_ns, size) changes, and a calendar change also clears the
field-array LRU. Rewriting or truncating existing bins while the backend runs
is unsupported.
"""

from __future__ import annotations

import os
import re
import threading
from bisect import bisect_left
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from math import isfinite
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.config.data_sources import QlibCNConfig
from app.data_sources.tencent import parse_tencent_kline_time
from app.utils.logger import get_logger

logger = get_logger(__name__)

_DAILY_FIELDS = ("open", "close", "high", "low", "volume")
_FIELD_CACHE_MAX = 1024
# Corruption-sanity bounds for the tail factor (NOT a semantic constraint --
# real factors observed up to ~3, and hfq-style conventions could run higher).
_FACTOR_TAIL_MIN = 1e-6
_FACTOR_TAIL_MAX = 1e6
# Qlib feature dirs look like sh600519 / sz000001 (and bj###### on some dumps);
# anything else must not reach the filesystem path below.
_QLIB_CODE_RE = re.compile(r"^[a-z]{2}\d{6}$")
# A-share close is 15:00 CST; allow a small buffer before today counts as completed.
_SESSION_END = time(15, 5)


def _warn_limited(key: str, message: str) -> None:
    """Log a warning at most once per day per key (quiet under retry storms)."""
    today = date.today()
    if _warn_days.get(key) == today:
        return
    _warn_days[key] = today
    logger.warning(message)


_warn_days: Dict[str, date] = {}


def _file_stamp(path: str) -> Optional[Tuple[int, int]]:
    """Cheap change detector for append-only text files (mtime_ns + size)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class QlibBinStore:
    """Read-only access to one qlib root: calendar/instruments cached with a
    file-stamp re-check (appends become visible), field arrays re-read per
    request with a small LRU on parsed values."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(os.path.expanduser(str(root or "").strip()))
        self._cal_lock = threading.Lock()
        self._cal_epochs: Optional[List[int]] = None
        self._cal_stamp: Optional[Tuple[int, int]] = None
        self._inst_lock = threading.Lock()
        self._instruments: Optional[Dict[str, List[Tuple[date, date]]]] = None
        self._inst_stamp: Optional[Tuple[int, int]] = None
        self._read_field_cached = lru_cache(maxsize=_FIELD_CACHE_MAX)(self._read_field_uncached)

    def validate(self) -> bool:
        """True when the root holds the minimum viable qlib layout."""
        return all(
            os.path.isfile(path)
            for path in (
                os.path.join(self.root, "calendars", "day.txt"),
                os.path.join(self.root, "instruments", "all.txt"),
            )
        ) and os.path.isdir(os.path.join(self.root, "features"))

    # -- calendar ---------------------------------------------------------

    def calendar_epochs(self) -> Optional[List[int]]:
        """Trading-day epochs parsed with the same local-timezone convention as
        Tencent rows, so cross-tier timestamps stay aligned. None = unusable.
        Re-reads day.txt when its stamp changes, so appended sessions become
        visible without a process restart."""
        path = os.path.join(self.root, "calendars", "day.txt")
        if self._cal_epochs is not None and self._cal_stamp == _file_stamp(path):
            return self._cal_epochs
        with self._cal_lock:
            if self._cal_epochs is not None and self._cal_stamp == _file_stamp(path):
                return self._cal_epochs
            stamp = _file_stamp(path)
            if stamp is None:
                return None
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    lines = [line.strip() for line in fp if line.strip()]
            except OSError:
                return None
            # An append landing mid-read would leave content and stamp
            # inconsistent; re-stat and give up (fall through) on a mismatch.
            if _file_stamp(path) != stamp:
                return None
            epochs: List[int] = []
            for line in lines:
                ts = parse_tencent_kline_time(line)
                if ts is None:
                    return None
                epochs.append(ts)
            if not epochs:
                return None
            if self._cal_epochs is not None and epochs != self._cal_epochs:
                # Calendar advanced (append): cached field arrays are stale.
                self._read_field_cached.cache_clear()
            self._cal_epochs = epochs
            self._cal_stamp = stamp
            return epochs

    def last_calendar_epoch(self) -> Optional[int]:
        epochs = self.calendar_epochs()
        return epochs[-1] if epochs else None

    # -- instruments ------------------------------------------------------

    def intervals(self, qlib_code: str) -> Optional[List[Tuple[date, date]]]:
        """Listing intervals for a code, or None when the code is unknown."""
        table = self._instrument_table()
        if table is None:
            return None
        return table.get(str(qlib_code).strip().lower())

    def _instrument_table(self) -> Optional[Dict[str, List[Tuple[date, date]]]]:
        path = os.path.join(self.root, "instruments", "all.txt")
        if self._instruments is not None and self._inst_stamp == _file_stamp(path):
            return self._instruments
        with self._inst_lock:
            if self._instruments is not None and self._inst_stamp == _file_stamp(path):
                return self._instruments
            stamp = _file_stamp(path)
            if stamp is None:
                return None
            table: Dict[str, List[Tuple[date, date]]] = {}
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    for line in fp:
                        parts = line.strip().split("\t")
                        if len(parts) != 3:
                            continue
                        try:
                            span = (
                                date.fromisoformat(parts[1].strip()),
                                date.fromisoformat(parts[2].strip()),
                            )
                        except ValueError:
                            continue
                        table.setdefault(parts[0].strip().lower(), []).append(span)
            except OSError:
                return None
            if not table:
                return None
            self._instruments = table
            self._inst_stamp = stamp
            return table

    # -- field arrays -----------------------------------------------------

    def read_field(self, qlib_code: str, field: str) -> Optional[Tuple[int, np.ndarray]]:
        """(calendar start index, values) for one field, or None when the bin
        is missing/corrupt. Cached per (code, field); the cache holds parsed
        arrays (~20KB each, maxsize 1024) so repeated backtest reads stay in
        memory."""
        code = str(qlib_code).strip().lower()
        field = str(field).strip().lower()
        if not _QLIB_CODE_RE.match(code) or not re.match(r"^[a-z]+$", field):
            return None
        return self._read_field_cached(code, field)

    def _read_field_uncached(self, qlib_code: str, field: str) -> Optional[Tuple[int, np.ndarray]]:
        epochs = self.calendar_epochs()
        if epochs is None:
            return None
        path = os.path.join(self.root, "features", qlib_code, f"{field}.day.bin")
        try:
            size = os.path.getsize(path)
            if size < 8 or size % 4 != 0:
                return None
            raw = np.fromfile(path, dtype="<f4")
        except (OSError, ValueError):
            return None
        if raw.size < 2 or raw.size * 4 != size:
            return None
        start_index = int(raw[0])
        values = raw[1:]
        if start_index < 0 or start_index + int(values.size) > len(epochs):
            return None
        return start_index, values


# -- module-level singleton ------------------------------------------------

_store: Optional[QlibBinStore] = None
_store_checked = False
_store_lock = threading.Lock()


def get_qlib_store() -> Optional[QlibBinStore]:
    """Process-wide store, built once. Disabled (None) unless
    QLIB_CN_DATA_DIR points at a valid qlib root."""
    global _store, _store_checked
    if _store_checked:
        return _store
    with _store_lock:
        if _store_checked:
            return _store
        root = QlibCNConfig.DATA_DIR
        if root:
            store = QlibBinStore(root)
            if store.validate():
                _store = store
                logger.info("qlib CN tier enabled at %s", store.root)
            else:
                logger.warning(
                    "QLIB_CN_DATA_DIR is set but not a valid qlib root, qlib CN tier disabled: %s",
                    root,
                )
        _store_checked = True
        return _store


def reset_qlib_store() -> None:
    """Test hook: drop the cached singleton so env/config changes re-evaluate."""
    global _store, _store_checked
    with _store_lock:
        _store = None
        _store_checked = False


# -- freshness ---------------------------------------------------------------

_xshg_calendar = None
_xshg_calendar_built_on: Optional[date] = None
_xshg_lock = threading.Lock()


def _get_xshg_calendar():
    global _xshg_calendar, _xshg_calendar_built_on
    today = date.today()
    if _xshg_calendar is not None and _xshg_calendar_built_on == today:
        return _xshg_calendar
    with _xshg_lock:
        if _xshg_calendar is not None and _xshg_calendar_built_on == today:
            return _xshg_calendar
        import exchange_calendars as exchange_calendars

        end = (today + timedelta(days=14)).isoformat()
        _xshg_calendar = exchange_calendars.get_calendar("XSHG", start="1999-01-01", end=end)
        _xshg_calendar_built_on = today
        return _xshg_calendar


def _last_completed_session_epoch() -> Optional[int]:
    """Epoch of the most recent fully completed XSHG session (local time past
    the 15:05 close buffer), or None when freshness cannot be determined."""
    try:
        sessions = _get_xshg_calendar().sessions.values  # np.datetime64[ns]
        now = datetime.now()
        today64 = np.datetime64(now.date(), "D")
        pos = int(np.searchsorted(sessions, today64))
        if pos < sessions.size and sessions[pos] == today64 and now.time() >= _SESSION_END:
            last = sessions[pos]
        else:
            pos -= 1
            if pos < 0:
                return None
            last = sessions[pos]
        return parse_tencent_kline_time(str(last)[:10])
    except Exception as exc:  # noqa: BLE001 - freshness must degrade to fall-through
        _warn_limited(
            "freshness", f"qlib CN tier: XSHG calendar unavailable, freshness unverifiable: {exc}"
        )
        return None


# -- fetch entry point -------------------------------------------------------

def fetch_qlib_daily_klines(
    tencent_code: str,
    timeframe: str,
    limit: int,
    before_time: Optional[int] = None,
    after_time: Optional[int] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Offline Tier 0 for CNStockDataSource.

    Returns None when this layer cannot fully satisfy the request (caller
    continues down the online chain), or the complete row list otherwise.
    """
    if timeframe not in ("1D", "1W"):
        return None
    store = get_qlib_store()
    if store is None:
        return None
    try:
        return _fetch(store, str(tencent_code or "").strip().lower(), timeframe, int(limit or 0),
                      before_time, after_time)
    except Exception as exc:  # noqa: BLE001 - tier must never break the online chain
        _warn_limited("fetch", f"qlib CN tier read failed, falling through: {exc}")
        return None


def _fetch(
    store: QlibBinStore,
    qlib_code: str,
    timeframe: str,
    limit: int,
    before_time: Optional[int],
    after_time: Optional[int],
) -> Optional[List[Dict[str, Any]]]:
    if not _QLIB_CODE_RE.match(qlib_code):
        return None
    epochs = store.calendar_epochs()
    if not epochs:
        return None

    # Unknown instrument (no listing intervals at all) -> fall through.
    intervals = store.intervals(qlib_code)
    if not intervals:
        return None

    # Window derivation (documented priority):
    #   after_time -> bisect; else before_time -> bisect back limit-1 sessions;
    #   else calendar end back limit-1 sessions. Always counted in trading days.
    last_idx = len(epochs) - 1
    if before_time is not None:
        end_idx = bisect_left(epochs, int(before_time)) - 1
    else:
        end_idx = last_idx
    if end_idx < 0:
        return None

    if after_time is not None:
        if int(after_time) < epochs[0]:
            return None  # front gap: would need stitching with another source
        start_idx = bisect_left(epochs, int(after_time))
        if start_idx > end_idx:
            return None
    elif timeframe == "1W":
        # limit counts weekly bars: walk back limit calendar weeks (7N days)
        # so aggregation yields at least limit buckets; filter_and_limit trims.
        if limit <= 0:
            return None
        start_idx = bisect_left(epochs, epochs[end_idx] - max(limit, 1) * 7 * 86400)
    else:
        if limit <= 0:
            return None
        start_idx = end_idx - (limit - 1)
    if start_idx < 0:
        return None

    # Listing-interval gate: the served window must intersect one of the
    # instrument's intervals (multi-interval aware; gaps mean not listed).
    win_start = datetime.fromtimestamp(epochs[start_idx]).date()
    win_end = datetime.fromtimestamp(epochs[end_idx]).date()
    if not any(win_start <= end and win_end >= start for start, end in intervals):
        return None

    # Coverage: every completed trading day inside the requested window must be
    # on the qlib calendar. min(before_time, last_completed) formalizes "touch
    # now" windows require a fresh calendar while historical backtest windows
    # do not. Strict by default; QLIB_CN_LENIENT=1 exempts the tail check.
    if QlibCNConfig.LENIENT:
        stale_after = parse_tencent_kline_time(date.today().isoformat())
        stale_after -= QlibCNConfig.STALENESS_DAYS * 86400
        if epochs[last_idx] < stale_after:
            _warn_limited(
                "stale",
                f"qlib CN tier: calendar is more than {QlibCNConfig.STALENESS_DAYS} days stale",
            )
    else:
        last_completed = _last_completed_session_epoch()
        if last_completed is None:
            return None  # freshness unverifiable -> refuse to serve
        required_end = min(int(before_time), last_completed) if before_time is not None else last_completed
        if epochs[last_idx] < required_end:
            _warn_limited(
                "tail",
                "qlib CN tier: calendar does not cover the last completed A-share session, "
                "falling through to online sources",
            )
            return None

    # Conversion to tail-anchored qfq. stored = raw x factor is a normalized
    # cumulative adjustment (factor ~0.24, never 1), so exposing stored values
    # as-is puts them at ~0.24x the online tiers' magnitude. Dividing by the
    # tail factor re-anchors at the latest session: the newest row equals the
    # raw price, continuity is preserved, and the online tiers stay comparable.
    expose_stored = QlibCNConfig.EXPOSE_STORED
    fields: Dict[str, Tuple[int, np.ndarray]] = {}
    for name in _DAILY_FIELDS:
        got = store.read_field(qlib_code, name)
        if got is None:
            return None
        fields[name] = got
    if not expose_stored:
        got = store.read_field(qlib_code, "factor")
        if got is None:
            _warn_limited(
                "factor-missing",
                "qlib CN tier: missing/unusable factor bin, falling through "
                "(set QLIB_CN_EXPOSE_STORED=1 to serve stored values, or rebuild the dump with factor bins)",
            )
            return None
        fields["factor"] = got
    # Fields may start at different calendar offsets (partial dumps). A field
    # starting after the requested window's first day means a data hole on the
    # left edge — fall through instead of silently serving a short series.
    field_base = max(start for start, _ in fields.values())
    if field_base > start_idx or field_base > end_idx:
        return None
    # Symmetric right-edge check: a field array ending before the window's
    # last day is a data hole -- fall through instead of indexing past it.
    if any(start + values.size <= end_idx for start, values in fields.values()):
        _warn_limited(
            "edge",
            "qlib CN tier: field bins end before the requested window, falling through",
        )
        return None

    price_scale = 1.0
    factor_start = 0
    factor_values: Optional[np.ndarray] = None
    if not expose_stored:
        factor_start, factor_values = fields["factor"]
        close_start, close_values = fields["close"]
        # Torn-generation gate: factor and close must come from the same dump
        # generation. A mixed-generation tree anchors f_last beyond (or short
        # of) the price data and silently mis-bases every served row.
        if factor_start != close_start or factor_values.size != close_values.size:
            _warn_limited(
                "generation",
                "qlib CN tier: factor and close bins are from different generations, falling through",
            )
            return None
        f_last = float(factor_values[-1])
        if not isfinite(f_last) or f_last <= 0.0 or not (_FACTOR_TAIL_MIN <= f_last <= _FACTOR_TAIL_MAX):
            _warn_limited(
                "factor-tail",
                "qlib CN tier: unusable tail factor, falling through",
            )
            return None
        price_scale = 1.0 / f_last
    else:
        _warn_limited(
            "expose-stored",
            "QLIB_CN_EXPOSE_STORED=1: serving raw stored values, adjustment basis unverified",
        )

    # Factor integrity on served rows only: generic qlib dumps fill suspension
    # rows with NaN across all fields (factor included), and those rows are
    # dropped below anyway -- but a non-finite or non-positive factor on a row
    # we WILL serve means the conversion would silently produce 0/negative
    # volume or mis-based prices. Check just the emitted rows. Each field is
    # sliced by its OWN start offset: partial dumps legitimately have fields
    # starting at different calendar positions, and a shared slice would
    # misalign the emit mask (or crash np.stack) for those roots.
    if factor_values is not None:
        win_factors = factor_values[start_idx - factor_start : end_idx - factor_start + 1]
        emit = np.ones(win_factors.size, dtype=bool)
        for name in ("open", "close", "high", "low"):
            field_start, field_values = fields[name]
            emit &= np.isfinite(field_values[start_idx - field_start : end_idx - field_start + 1])
        served_factors = win_factors[emit]
        if served_factors.size and (
            not bool(np.isfinite(served_factors).all()) or not bool((served_factors > 0).all())
        ):
            _warn_limited(
                "factor-window",
                "qlib CN tier: invalid factor on a served row, falling through",
            )
            return None

    rows: List[Dict[str, Any]] = []
    for pos in range(start_idx, end_idx + 1):
        o, c, h, low, vol = (
            float(fields[name][1][pos - fields[name][0]])
            for name in ("open", "close", "high", "low", "volume")
        )
        if not (isfinite(o) and isfinite(c) and isfinite(h) and isfinite(low)):
            continue  # suspended / missing day
        if factor_values is not None:
            vol = vol * float(factor_values[pos - factor_start])
        rows.append(
            {
                "time": epochs[pos],
                "open": round(o * price_scale, 4),
                "high": round(h * price_scale, 4),
                "low": round(low * price_scale, 4),
                "close": round(c * price_scale, 4),
                "volume": round(vol if isfinite(vol) else 0.0, 2),
            }
        )
    if not rows:
        return None
    if timeframe == "1W":
        rows = _aggregate_weekly(rows)
        if not rows:
            return None
    return rows


def _aggregate_weekly(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group daily rows into calendar weeks (Monday-based, matching the
    Tencent weekly tier — the ISO new-year week 12/30..01/03 stays one bar);
    the weekly bar's time is the last daily bar of the week. When the served
    window starts mid-week the first bucket is a partial week (only the days
    inside the window), which can differ from vendors that always emit full
    calendar weeks."""
    out: List[Dict[str, Any]] = []
    bucket: Optional[Dict[str, Any]] = None
    bucket_key: Optional[date] = None
    for row in rows:
        d = datetime.fromtimestamp(row["time"]).date()
        key = d - timedelta(days=d.weekday())  # Monday of the calendar week
        if bucket is None or key != bucket_key:
            if bucket is not None:
                out.append(bucket)
            bucket = dict(row)
            bucket_key = key
        else:
            bucket["high"] = max(bucket["high"], row["high"])
            bucket["low"] = min(bucket["low"], row["low"])
            bucket["close"] = row["close"]
            bucket["volume"] = round(bucket["volume"] + row["volume"], 2)
            bucket["time"] = row["time"]
    if bucket is not None:
        out.append(bucket)
    return out
