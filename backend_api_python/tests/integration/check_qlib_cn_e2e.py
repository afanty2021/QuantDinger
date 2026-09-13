#!/usr/bin/env python
"""End-to-end check: CNStock kline API served from a real local qlib data dir.

Boots the real Flask app (full blueprint stack) with QLIB_CN_DATA_DIR pointed
at a live qlib root, then drives the human /api/kline endpoint through the
Flask test client (routing, request guards, JSON envelopes) and cross-checks
every returned bar against an INDEPENDENT raw read of the qlib bin files
(calendar + np.fromfile, no shared code with app.data_sources.qlib_cn).

Online tiers (Twelve Data / Tencent / yfinance / AkShare) are stubbed at the
data-source boundary with call recorders, so a pass proves the response came
from the qlib tier -- and the fall-through case proves the online chain is
still wired when qlib cannot serve.

Required env:
    DATABASE_URL       ephemeral PostgreSQL with migrated schema
    QLIB_CN_DATA_DIR   real qlib root (calendars/instruments/features)
Optional env:
    REDIS_HOST/REDIS_PORT  ephemeral Redis (cache disabled anyway)
    QLIB_E2E_LENIENT=1 or --lenient
                         run with QLIB_CN_LENIENT=1 (stale-tail exemption) —
                         use this when the local qlib root is known-stale and
                         you only want to prove the serving path end-to-end.

Usage:
    DATABASE_URL=postgresql://quantdinger_test:quantdinger_test@127.0.0.1:54329/quantdinger_test \
    QLIB_CN_DATA_DIR=~/.qlib/qlib_data/cn_data \
    python tests/integration/check_qlib_cn_e2e.py [--lenient]
"""
import os
import sys
from datetime import datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("SECRET_KEY", "e2e-secret-key-for-integration-tests-32b")
os.environ.setdefault("ADMIN_USER", "e2eadmin")
os.environ.setdefault("ADMIN_PASSWORD", "e2epass123")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("SKIP_STARTUP_HOOKS", "1")
os.environ.setdefault("CELERY_TASKS_ENABLED", "false")

LENIENT = os.environ.get("QLIB_E2E_LENIENT", "0") == "1" or "--lenient" in sys.argv
if LENIENT:
    os.environ["QLIB_CN_LENIENT"] = "1"

if not os.environ.get("QLIB_CN_DATA_DIR"):
    print("QLIB_CN_DATA_DIR is required (real qlib root)")
    sys.exit(2)
if not os.environ.get("DATABASE_URL"):
    print("DATABASE_URL is required (migrated PostgreSQL)")
    sys.exit(2)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def epoch(day_str: str) -> int:
    return int(datetime.strptime(day_str, "%Y-%m-%d").timestamp())


# ---------------------------------------------------------------------------
# Independent qlib bin reader (deliberately does NOT reuse qlib_cn helpers)
# ---------------------------------------------------------------------------

def raw_daily_closes(root, qlib_code):
    """Independent read in the SAME exposure basis as the tier: tail-anchored
    qfq prices (stored / f_last, rounded per row) and raw volumes (stored *
    f, rounded per row). The E2E compares exact values, so the rounding
    recipe here must mirror the serving pipeline."""
    import numpy as np

    with open(os.path.join(root, "calendars", "day.txt"), encoding="utf-8") as fp:
        days = [ln.strip() for ln in fp if ln.strip()]

    def read(field):
        path = os.path.join(root, "features", qlib_code, f"{field}.day.bin")
        arr = np.fromfile(path, dtype="<f4")
        start = int(arr[0])
        return days[start : start + len(arr) - 1], arr[1:]

    close_days, close = read("close")
    _, vol = read("volume")
    _, factor = read("factor")
    f_last = float(factor[-1])
    qfq = [round(float(c) / f_last, 4) for c in close]
    vols = [round(float(v) * float(f), 2) for v, f in zip(vol, factor)]
    return list(zip(close_days, qfq, vols))


def last_n_trading_days(days, n, before_day=None):
    if before_day is not None:
        days = [d for d in days if d < before_day]
    return days[-n:]


def last_completed_xshg_session():
    """Independent mirror of the tier's strict-freshness rule (XSHG calendar +
    15:05 close buffer); deliberately shares no code with qlib_cn."""
    try:
        import exchange_calendars
        import numpy as np

        end = (datetime.now() + timedelta(days=1)).date().isoformat()
        sessions = exchange_calendars.get_calendar("XSHG", start="1999-01-01", end=end).sessions.values
        now = datetime.now()
        today64 = np.datetime64(now.date(), "D")
        pos = int(np.searchsorted(sessions, today64))
        if pos < sessions.size and sessions[pos] == today64 and now.time() >= time(15, 5):
            last = sessions[pos]
        elif pos >= 1:
            last = sessions[pos - 1]
        else:
            return None
        return str(last)[:10]
    except Exception as exc:  # noqa: BLE001 - report the rule, not the crash
        print(f"[WARN] cannot determine last completed XSHG session: {exc}")
        return None


# ---------------------------------------------------------------------------

def main():
    from app.data_sources import cn_stock
    from app import create_app

    # Stub every online tier with recorders BEFORE any request is served.
    online_calls = []

    def _stub(name, result):
        def _fn(*args, **kwargs):
            online_calls.append(name)
            return result

        return _fn

    cn_stock.fetch_twelvedata_klines = _stub("twelvedata", [])
    cn_stock.fetch_kline = _stub("tencent", [])
    cn_stock.fetch_yfinance_klines = _stub("yfinance", [])
    cn_stock.fetch_akshare_minute_klines = _stub("akshare-minute", [])
    cn_stock.fetch_akshare_weekly_klines = _stub("akshare-weekly", [])

    app = create_app()
    client = app.test_client()

    root = os.path.expanduser(os.environ["QLIB_CN_DATA_DIR"])
    reference = {
        "sh600519": raw_daily_closes(root, "sh600519"),
        "sz000001": raw_daily_closes(root, "sz000001"),
    }
    last_day = reference["sh600519"][-1][0]
    print(f"qlib root: {root} (calendar ends {last_day})")

    def kline(symbol, timeframe="1D", limit=5, before_time=None, market="CNStock"):
        params = {"market": market, "symbol": symbol, "timeframe": timeframe, "limit": limit}
        if before_time is not None:
            params["before_time"] = str(before_time)
        resp = client.get("/api/indicator/kline", query_string=params)
        body = resp.get_json()
        return resp.status_code, body

    # -- Case 1: daily bars served by qlib tier, values match raw bins ------
    online_calls.clear()
    status, body = kline("600519", "1D", 5)
    data = (body or {}).get("data") or []
    check("1D /api/kline returns 200+code=1", status == 200 and (body or {}).get("code") == 1,
          f"http={status} code={(body or {}).get('code')}")
    check("1D returns exactly 5 bars", len(data) == 5, f"got {len(data)}")
    expected = reference["sh600519"][-5:]
    got = [
        (datetime.fromtimestamp(r["time"]).strftime("%Y-%m-%d"), round(float(r["close"]), 4), round(float(r["volume"]), 2))
        for r in data
    ]
    check("1D bars match independent raw bin read (date/qfq-close/raw-volume)", got == expected,
          f"got={got[-1] if got else None} want={expected[-1] if expected else None}")
    check("1D online tiers untouched (data provenance = qlib)", online_calls == [],
          f"calls={online_calls}")
    check("1D meta.bar_count", (body or {}).get("meta", {}).get("bar_count") == 5)

    # -- Case 2: weekly aggregation over real data --------------------------
    online_calls.clear()
    status, body = kline("600519", "1W", 8)
    data = (body or {}).get("data") or []
    check("1W returns 200+code=1", status == 200 and (body or {}).get("code") == 1)
    check("1W online tiers untouched", online_calls == [], f"calls={online_calls}")
    if data:
        last_w = datetime.fromtimestamp(data[-1]["time"])
        check("1W last bar is the last trading day (Friday label)", last_w.strftime("%Y-%m-%d") == last_day,
              f"last weekly bar={last_w.date()}")
        check("1W bars labeled on Fridays", all(datetime.fromtimestamp(r["time"]).weekday() == 4 for r in data))
        # volume of the last week must equal the sum of that calendar week's dailies
        last_week_monday = last_w.date() - timedelta(days=last_w.date().weekday())
        week_sum = round(sum(v for d, _, v in reference["sh600519"]
                             if datetime.strptime(d, "%Y-%m-%d").date() >= last_week_monday), 2)
        check("1W volume = sum of the week's daily volume",
              round(float(data[-1]["volume"]), 2) == week_sum,
              f"weekly={data[-1]['volume']} daily_sum={week_sum}")
    else:
        check("1W returned bars", False, "empty data")

    # -- Case 3: before_time window -----------------------------------------
    online_calls.clear()
    cutoff = epoch(last_day) - 86400 * 5  # ~5 days before the last session
    status, body = kline("600519", "1D", 300, before_time=cutoff)
    data = (body or {}).get("data") or []
    check("window query returns 200+code=1", status == 200 and (body or {}).get("code") == 1)
    check("window query: all bars strictly before cutoff",
          all(r["time"] < cutoff for r in data), f"cutoff={cutoff} last={data[-1]['time'] if data else None}")
    check("window query served by qlib (online untouched)", online_calls == [], f"calls={online_calls}")

    # -- Case 4: second symbol (sz000001) ------------------------------------
    online_calls.clear()
    status, body = kline("000001", "1D", 3)
    data = (body or {}).get("data") or []
    expected = reference["sz000001"][-3:]
    got = [
        (datetime.fromtimestamp(r["time"]).strftime("%Y-%m-%d"), round(float(r["close"]), 4))
        for r in data
    ]
    check("000001 served by qlib, closes match raw bins", status == 200 and got == [e[:2] for e in expected],
          f"got={got} want={[e[:2] for e in expected]}")
    check("000001 online tiers untouched", online_calls == [], f"calls={online_calls}")

    # -- Case 5: unknown symbol falls through to the online chain ------------
    online_calls.clear()
    status, body = kline("999999", "1D", 5)
    check("unknown symbol: online tier consulted (fallback intact)", "tencent" in online_calls,
          f"calls={online_calls}")
    check("unknown symbol: empty result with code=0",
          (body or {}).get("code") == 0 and (body or {}).get("data") == [],
          f"msg={(body or {}).get('msg')}")

    # -- Case 6: freshness precondition matches the tier's actual rule -------
    # Strict mode serves only when the calendar covers the last completed XSHG
    # session — the same rule Cases 1-4 depend on, not a proxy like "gap <= 7d".
    if LENIENT:
        check("lenient mode: stale-tail exemption active", True,
              "QLIB_CN_LENIENT=1 — Cases 1-5 above prove the serving path")
    else:
        last_completed = last_completed_xshg_session()
        check("calendar covers the last completed A-share session (strict mode)",
              last_completed is not None and last_day >= last_completed,
              f"calendar_end={last_day} last_completed={last_completed}")

    print()
    if FAILURES:
        print(f"E2E FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("E2E PASSED: qlib tier serves the real API end-to-end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
