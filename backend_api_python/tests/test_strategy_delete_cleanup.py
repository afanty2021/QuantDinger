from pathlib import Path

import pytest

from app.services import strategy as strategy_service_module
from app.services.strategy import StrategyDeleteBlocked, StrategyService


class _Cursor:
    def __init__(self, *, strategy_exists=True, active_lease=False, active_command=False):
        self.strategy_exists = strategy_exists
        self.active_lease = active_lease
        self.active_command = active_command
        self.current_row = None
        self.rowcount = 0
        self.statements = []

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.statements.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith("SELECT id FROM qd_strategies_trading"):
            self.current_row = {"id": 42} if self.strategy_exists else None
        elif "FROM qd_strategy_runtime_leases" in normalized and normalized.startswith("SELECT"):
            self.current_row = {"present": 1} if self.active_lease else None
        elif "FROM qd_strategy_commands" in normalized and normalized.startswith("SELECT"):
            self.current_row = {"present": 1} if self.active_command else None
        else:
            self.current_row = None
        if normalized.startswith("DELETE FROM qd_strategies_trading"):
            self.rowcount = 1

    def fetchone(self):
        return self.current_row

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _statement_text(cursor):
    return "\n".join(sql for sql, _params in cursor.statements)


def test_delete_strategy_removes_all_runtime_references_in_one_transaction(monkeypatch):
    cursor = _Cursor()
    connection = _Connection(cursor)
    monkeypatch.setattr(strategy_service_module, "get_db_connection", lambda: connection)

    assert StrategyService().delete_strategy(42, user_id=7) is True

    sql = _statement_text(cursor)
    for table in (
        "pending_orders",
        "qd_live_order_bindings",
        "strategy_runtime_locks",
        "strategy_order_fills",
        "strategy_order_intents",
        "strategy_runtime_state",
        "strategy_runtime_events",
        "strategy_runs",
        "qd_strategy_commands",
        "qd_strategy_runtime_leases",
    ):
        assert f"DELETE FROM {table}" in sql
    assert "UPDATE qd_execution_events AS event" in sql
    assert "NULLIF(payload_json, '')::jsonb" in sql
    assert (42, "42") in [params for _statement, params in cursor.statements]
    assert "UPDATE qd_strategy_commands SET status = 'cancelled'" in sql
    assert "UPDATE qd_backtest_runs SET strategy_id = NULL" in sql
    assert "UPDATE qd_backtest_trades SET strategy_id = NULL" in sql
    assert "UPDATE qd_indicator_codes SET source_strategy_id = NULL" in sql
    assert sql.index("DELETE FROM qd_live_order_bindings") < sql.index("DELETE FROM qd_strategies_trading")
    assert connection.committed is True
    assert connection.rolled_back is False


@pytest.mark.parametrize("active_lease,active_command", [(True, False), (False, True)])
def test_delete_strategy_refuses_active_runtime_work(monkeypatch, active_lease, active_command):
    cursor = _Cursor(active_lease=active_lease, active_command=active_command)
    connection = _Connection(cursor)
    monkeypatch.setattr(strategy_service_module, "get_db_connection", lambda: connection)

    with pytest.raises(StrategyDeleteBlocked, match="strategyV2.stopBeforeDelete"):
        StrategyService().delete_strategy(42, user_id=7)

    assert "DELETE FROM qd_strategies_trading" not in _statement_text(cursor)
    assert connection.committed is False
    assert connection.rolled_back is True


def test_delete_strategy_returns_false_without_touching_orphans_for_wrong_owner(monkeypatch):
    cursor = _Cursor(strategy_exists=False)
    connection = _Connection(cursor)
    monkeypatch.setattr(strategy_service_module, "get_db_connection", lambda: connection)

    assert StrategyService().delete_strategy(42, user_id=999) is False

    assert "DELETE FROM qd_live_order_bindings" not in _statement_text(cursor)
    assert connection.committed is False
    assert connection.rolled_back is True


def test_schema_installs_strategy_delete_trigger_without_history_repair():
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    sql = (migrations / "init.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION qd_cleanup_deleted_strategy()" in sql
    assert "BEFORE DELETE ON qd_strategies_trading" in sql
    assert "NULLIF(payload_json, '')::jsonb" in sql
    assert "process_error = 'strategy_deleted'" in sql
    assert "Repair orphan rows created before deletion cleanup became transactional" not in sql
    assert not (migrations / "20261001_strategy_delete_cleanup.sql").exists()
