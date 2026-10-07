from datetime import datetime, timezone

from app.services.strategy_daily_pnl import (
    _load_current_equity,
    choose_opening_equity,
    resolve_business_day_window,
)


def test_business_day_window_uses_client_iana_timezone():
    start, end, name = resolve_business_day_window(
        now=datetime(2026, 7, 21, 18, 30, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )

    assert name == "Asia/Shanghai"
    assert start == datetime(2026, 7, 21, 16, 0)
    assert end == datetime(2026, 7, 22, 16, 0)


def test_opening_equity_prefers_nearest_midnight_snapshot():
    day_start = datetime(2026, 7, 21, 16, 0)
    opening, estimated, source = choose_opening_equity(
        day_start=day_start,
        before={"equity": 1010, "captured_at": datetime(2026, 7, 21, 15, 58)},
        after={"equity": 1011, "captured_at": datetime(2026, 7, 21, 16, 4)},
        reconstructed=999,
    )

    assert opening == 1010
    assert estimated is False
    assert source == "snapshot_before"


def test_opening_equity_marks_ledger_reconstruction_as_estimated():
    opening, estimated, source = choose_opening_equity(
        day_start=datetime(2026, 7, 21, 16, 0),
        before=None,
        after=None,
        reconstructed=987.5,
    )

    assert opening == 987.5
    assert estimated is True
    assert source == "ledger_reconstruction"


def test_current_equity_query_escapes_literal_percent_for_postgres(monkeypatch):
    class Cursor:
        def execute(self, query, params):
            query % tuple(repr(value) for value in params)

        def fetchall(self):
            return []

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    class ConnectionContext:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.services.strategy_daily_pnl.get_db_connection",
        lambda: ConnectionContext(),
    )

    assert _load_current_equity([1], user_id=2) == {}
