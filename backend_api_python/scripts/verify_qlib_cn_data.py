#!/usr/bin/env python
"""Verify qlib CN bin adjustment semantics against Tencent raw prices.

qlib's dump pipeline stores adjusted values: stored_price = raw_price * factor
and stored_volume = raw_volume / factor. This script samples recent rows of one
symbol from a local qlib root, reconstructs the raw price/volume
(raw = stored / factor for price, raw = stored * factor for volume) and
compares them with Tencent's unadjusted (adj="") kline. Run it after building
or replacing a qlib data directory:

    QLIB_CN_DATA_DIR=~/.qlib/qlib_data/cn_data \
        python scripts/verify_qlib_cn_data.py --symbol 600519

Exit code 0 = direction confirmed, 1 = mismatch or unusable data.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.data_sources import QlibCNConfig  # noqa: E402
from app.data_sources.qlib_cn import QlibBinStore  # noqa: E402
from app.data_sources.tencent import fetch_kline, tencent_kline_rows_to_dicts  # noqa: E402

REL_TOLERANCE = 0.01  # vendors differ slightly on raw values; 1% is decisive for direction


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519", help="A-share symbol, e.g. 600519")
    parser.add_argument("--samples", type=int, default=5, help="recent rows to compare")
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
    fields = {}
    for name in ("close", "volume", "factor"):
        got = store.read_field(qlib_code, name)
        if got is None:
            print(f"missing/unusable bin: features/{qlib_code}/{name}.day.bin")
            return 1
        fields[name] = got

    epochs = store.calendar_epochs()
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
