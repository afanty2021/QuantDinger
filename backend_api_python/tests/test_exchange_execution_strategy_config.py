import pytest

from app.services import exchange_execution


class _Cursor:
    def execute(self, _sql, _params):
        return None

    def fetchone(self):
        return None

    def close(self):
        return None


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor()


def test_load_strategy_configs_rejects_missing_strategy(monkeypatch):
    monkeypatch.setattr(exchange_execution, "get_db_connection", lambda: _Connection())

    with pytest.raises(LookupError, match="strategyV2.strategyNotFound"):
        exchange_execution.load_strategy_configs(738)
