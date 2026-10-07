from __future__ import annotations

from app.services import pending_order_worker as worker_module
from app.services import pending_order_position_sync as sync_module


class _Cursor:
    def __init__(self):
        self._rows = []

    def execute(self, sql, _params=None):
        if "FROM qd_strategy_positions" in sql:
            self._rows = [
                {
                    "id": 1,
                    "strategy_id": 76,
                    "symbol": "SPY",
                    "side": "long",
                    "size": 0,
                    "entry_price": 0,
                }
            ]
        elif "FROM qd_strategies_trading" in sql:
            self._rows = []
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _Database:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def cursor():
        return _Cursor()


def test_position_sync_skips_stopped_live_strategy_rows(monkeypatch):
    monkeypatch.setattr(sync_module, "get_db_connection", lambda: _Database())
    monkeypatch.setattr(
        sync_module,
        "load_strategy_configs",
        lambda _strategy_id: {
            "status": "stopped",
            "execution_mode": "live",
            "user_id": 3,
            "exchange_config": {"exchange_id": "alpaca", "credential_id": 1},
        },
    )
    monkeypatch.setattr(sync_module, "should_skip_position_sync", lambda _strategy_id: False)
    create_client_calls = []
    monkeypatch.setattr(
        sync_module,
        "create_client",
        lambda *_args, **_kwargs: create_client_calls.append(True),
    )
    worker = object.__new__(worker_module.PendingOrderWorker)

    worker._sync_positions_best_effort()

    assert create_client_calls == []
