#!/usr/bin/env python
"""Verify qlib CN bin adjustment semantics against Tencent prices.

qlib's dump pipeline stores adjusted values: stored_price = raw_price * factor
and stored_volume = raw_volume / factor. This script samples recent rows of one
symbol from a local qlib root, reconstructs the raw price/volume
(raw = stored / factor for price, raw = stored * factor for volume) and
compares them with Tencent's unadjusted (adj="") kline. Run it after building
or replacing a qlib data directory:

    QLIB_CN_DATA_DIR=~/.qlib/qlib_data/cn_data \
        python scripts/verify_qlib_cn_data.py --symbol 600519

With --qfq it additionally checks the tail-anchored qfq exposure basis
(stored / f_last) against Tencent's qfq kline: the newest date-aligned row is
hard-asserted (relative diff <= 0.05% -- at the anchor both sides equal the
raw price), older rows are printed for reference only (the two vendors'
adjustment methods diverge with accumulated dividend events; 0.33% over four
months is expected). Preconditions: the qlib calendar must cover the last
completed A-share session (a stale root fails explicitly -- its "latest" row
would not be comparable), and avoid running on an ex-dividend day before the
pack lands (the anchor-row gap then equals the full dividend).

Exit code 0 = checks passed, 1 = mismatch or unusable data.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.data_sources import QlibCNConfig  # noqa: E402
from app.data_sources.qlib_cn import QlibBinStore, _last_completed_session_epoch  # noqa: E402
from app.data_sources.tencent import fetch_kline, tencent_kline_rows_to_dicts  # noqa: E402

REL_TOLERANCE = 0.01  # vendors differ slightly on raw values; 1% is decisive for direction
QFQ_ANCHOR_TOLERANCE = 0.0005  # at the anchor both vendors equal the raw price


def check_qfq_basis(fields: dict, epochs: list, code: str, samples: int) -> int:
    """Tail-anchored qfq exposure parity vs Tencent qfq. Returns exit code."""
    f_start, f_values = fields["factor"]
    c_start, c_values = fields["close"]
    # Generation pairing, mirroring the serving-layer gate: a torn pack would
    # anchor f_last beyond (or short of) the price data with per-row error too
    # small for the tolerance below to catch.
    if (f_start, f_values.size) != (c_start, c_values.size):
        print("factor and close bins are from different generations "
              f"(factor start={f_start} size={f_values.size}, close start={c_start} "
              f"size={c_values.size}) -- rebuild the data directory from a complete pack")
        return 1

    last_calendar_day = datetime.fromtimestamp(epochs[-1]).date().isoformat()
    last_completed = _last_completed_session_epoch()
    if last_completed is None:
        print("[WARN] cannot determine the last completed A-share session "
              "(exchange calendar unavailable); stale-root precondition NOT checked "
              "-- check the qlib root's calendars/ directory and the installed "
              "exchange-calendars package")
    elif epochs[-1] < last_completed:
        print(f"qlib root is stale: calendar ends {last_calendar_day} but the last "
              f"completed A-share session is newer -- the anchor-row comparison is "
              f"meaningless. Update the data directory first.")
        return 1

    anchor_pos = f_start + f_values.size - 1  # calendar index of the factor tail
    f_last = float(f_values[-1])
    qfq_rows = tencent_kline_rows_to_dicts(fetch_kline(code, period="day", count=120, adj="qfq"))
    tencent_by_day = {
        datetime.fromtimestamp(row["time"]).date().isoformat(): row for row in qfq_rows
    }

    checked = 0
    anchor_checked = False
    failures = 0
    for pos in range(len(epochs) - 1, -1, -1):
        if checked >= samples:
            break
        if pos < f_start or pos >= f_start + f_values.size:
            continue
        day = datetime.fromtimestamp(epochs[pos]).date().isoformat()
        tencent = tencent_by_day.get(day)
        if tencent is None or tencent["close"] <= 0:
            continue
        stored_close = float(fields["close"][1][pos - fields["close"][0]])
        qfq_exposed = stored_close / f_last
        rel = abs(qfq_exposed - tencent["close"]) / tencent["close"]
        is_anchor = pos == anchor_pos
        ok = rel <= (QFQ_ANCHOR_TOLERANCE if is_anchor else REL_TOLERANCE)
        if is_anchor:
            anchor_checked = True
            if not ok:
                failures += 1
        mark = "OK " if ok else "DRIFT"  # reference rows never fail the run
        note = " [anchor: hard assert]" if is_anchor else " [reference only]"
        print(
            f"{mark} {day} exposed_qfq={qfq_exposed:.4f} (tencent qfq {tencent['close']:.4f}) "
            f"rel={rel * 100:.4f}%{note}"
        )
        checked += 1

    if checked == 0 or not anchor_checked:
        print("anchor row was never compared (no date overlap with the Tencent qfq kline at the "
              "factor tail); the exposure basis is NOT verified")
        return 1
    if failures:
        print("anchor-row qfq exposure mismatch. Likely causes: (a) ex-dividend day with the "
              "pack not yet updated (gap ~= the full dividend, rerun after the EOD pack lands); "
              "(b) torn generation between close and factor bins; (c) corrupted factor tail.")
        return 1
    print(f"qfq exposure verified on {checked} rows (anchor hard-asserted at "
          f"{QFQ_ANCHOR_TOLERANCE * 100:.2f}%)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519", help="A-share symbol, e.g. 600519")
    parser.add_argument("--samples", type=int, default=5, help="recent rows to compare")
    parser.add_argument("--qfq", action="store_true",
                        help="also verify the tail-anchored qfq exposure basis vs Tencent qfq")
    args = parser.parse_args()

    root = QlibCNConfig.DATA_DIR
    if not root:
        print("QLIB_CN_DATA_DIR is not set; pass a configured environment or set it in env")
        return 1
    store = QlibBinStore(root)
    if not store.validate():
        print(f"not a valid qlib root: {root}")
        return 1

    code = f"sh{args.symbol}" if args.symbol.startswith("6") else f"sz{args.symbol}"
    qlib_code = code.lower()
    names = ("close", "volume", "factor")
    fields = {}
    for name in names:
        got = store.read_field(qlib_code, name)
        if got is None:
            print(f"missing/unusable bin: features/{qlib_code}/{name}.day.bin")
            return 1
        fields[name] = got
    epochs = store.calendar_epochs()
    if not epochs:
        print("unusable calendar")
        return 1

    if args.qfq:
        rc = check_qfq_basis(fields, epochs, code, args.samples)
        if rc != 0:
            return rc

    raw_rows = tencent_kline_rows_to_dicts(fetch_kline(code, period="day", count=120, adj=""))
    tencent_by_day = {
        datetime.fromtimestamp(row["time"]).date().isoformat(): row for row in raw_rows
    }

    checked = 0
    mismatches = 0
    for pos in range(len(epochs) - 1, 0, -1):
        if checked >= args.samples:
            break
        day = datetime.fromtimestamp(epochs[pos]).date().isoformat()
        tencent = tencent_by_day.get(day)
        if tencent is None:
            continue
        factor = float(fields["factor"][1][pos - fields["factor"][0]])
        stored_close = float(fields["close"][1][pos - fields["close"][0]])
        stored_volume = float(fields["volume"][1][pos - fields["volume"][0]])
        if factor <= 0 or stored_close <= 0 or tencent["close"] <= 0:
            continue
        # Inverse of the dump pipeline: raw_price = stored / factor,
        # raw_volume = stored * factor.
        raw_close = stored_close / factor
        raw_volume = stored_volume * factor
        price_ok = abs(raw_close - tencent["close"]) / tencent["close"] <= REL_TOLERANCE
        volume_ok = (
            abs(raw_volume - tencent["volume"]) / tencent["volume"] <= REL_TOLERANCE
            if tencent["volume"] > 0
            else True
        )
        status = "OK " if (price_ok and volume_ok) else "FAIL"
        if not (price_ok and volume_ok):
            mismatches += 1
        print(
            f"{status} {day} factor={factor:.6f} raw_close={raw_close:.4f} "
            f"(tencent {tencent['close']:.4f}) raw_volume={raw_volume:.0f} "
            f"(tencent {tencent['volume']:.0f})"
        )
        checked += 1

    if checked == 0:
        print("no overlapping rows between qlib calendar and Tencent kline; nothing verified")
        return 1
    if mismatches:
        print(f"{mismatches}/{checked} rows mismatch — stored values do not follow "
              "raw = adjusted / factor (price), raw = adjusted * factor (volume)")
        return 1
    print(f"direction verified on {checked} rows: price=adjusted×factor, volume=adjusted÷factor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
