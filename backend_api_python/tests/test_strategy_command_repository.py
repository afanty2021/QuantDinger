from __future__ import annotations

from app.services import strategy_command_repository as repository_module
from app.services.strategy_command_repository import StrategyCommandRepository


def _command_row(*, close_positions: bool) -> dict:
    return {
        "id": 17,
        "strategy_id": 55,
        "user_id": 1,
        "command_type": "stop",
        "status": "pending",
        "idempotency_key": "stop-55",
        "payload_json": {"close_positions": close_positions},
        "result_json": {},
        "attempts": 0,
        "error_message": "",
    }


class FakeCursor:
    def __init__(self):
        self.executions = []
        self.current_sql = ""

    def execute(self, sql, params):
        self.current_sql = sql
        self.executions.append((sql, params))

    def fetchone(self):
        if "SELECT * FROM qd_strategy_commands" in self.current_sql:
            return _command_row(close_positions=False)
        if "SET payload_json" in self.current_sql:
            return _command_row(close_positions=True)
        return None

    def close(self):
        return None


class FakeConnection:
    def __init__(self):
        self.cursor_value = FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        return None

    def rollback(self):
        return None


def test_pending_pause_is_upgraded_to_pause_and_close(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(repository_module, "get_db_connection", lambda: connection)

    command = StrategyCommandRepository().enqueue(
        strategy_id=55,
        command_type="stop",
        user_id=1,
        payload={"close_positions": True},
        idempotency_key="stop-55-close",
    )

    assert command.payload["close_positions"] is True
    upgrade_sql, upgrade_params = connection.cursor_value.executions[2]
    assert "SET payload_json" in upgrade_sql
    assert upgrade_params == ('{"close_positions": true}', 17)
