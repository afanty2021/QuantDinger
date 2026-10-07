from __future__ import annotations

import pytest

from app.services import strategy_command_repository as repository_module
from app.services import event_inbox as event_inbox_module
from app.services.event_inbox import StrategyShardLeaseRepository
from app.services.strategy_command_repository import StrategyCommandRepository
from app.services.strategy_v2 import live_execution
from app.services.strategy_v2.live_execution import LiveOrderRequest, StrategyV2OrderGateway


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.executions = []
        self.rowcount = 1

    def execute(self, sql, params):
        self.executions.append((sql, params))

    def fetchone(self):
        return self.row

    def close(self):
        return None


class FakeConnection:
    def __init__(self, row):
        self.cursor_value = FakeCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_value


def _request(token):
    return LiveOrderRequest(
        strategy_id=11,
        strategy_run_id=21,
        user_id=1,
        symbol="BTC/USDT",
        action="open_long",
        quantity=0.01,
        reference_price=100,
        signal_timestamp=123,
        market_type="swap",
        execution_mode="live",
        runtime_fencing_token=token,
    )


def test_order_gateway_accepts_current_runtime_fence(monkeypatch):
    connection = FakeConnection({"?column?": 1})
    monkeypatch.setattr(live_execution, "get_db_connection", lambda: connection)

    StrategyV2OrderGateway._assert_runtime_fence(_request(7))

    assert connection.cursor_value.executions[0][1] == (11, 7)


def test_order_gateway_rejects_stale_runtime_fence(monkeypatch):
    monkeypatch.setattr(
        live_execution,
        "get_db_connection",
        lambda: FakeConnection(None),
    )

    with pytest.raises(RuntimeError, match="staleFencingToken"):
        StrategyV2OrderGateway._assert_runtime_fence(_request(6))


def test_manual_control_order_can_omit_runtime_fence(monkeypatch):
    monkeypatch.setattr(
        live_execution,
        "get_db_connection",
        lambda: (_ for _ in ()).throw(AssertionError("database should not be read")),
    )

    StrategyV2OrderGateway._assert_runtime_fence(_request(0))


def test_releasing_runtime_lease_preserves_fencing_row(monkeypatch):
    connection = FakeConnection(None)
    connection.commit = lambda: None
    monkeypatch.setattr(repository_module, "get_db_connection", lambda: connection)

    StrategyCommandRepository().release_strategy_lease(
        strategy_id=11,
        owner_id="worker-a",
    )

    sql, params = connection.cursor_value.executions[0]
    assert "UPDATE qd_strategy_runtime_leases" in sql
    assert "owner_id = ''" in sql
    assert "DELETE FROM qd_strategy_runtime_leases" not in sql
    assert params == (11, "worker-a")


def test_revoking_runtime_lease_invalidates_any_owner(monkeypatch):
    connection = FakeConnection(None)
    connection.commit = lambda: None
    monkeypatch.setattr(repository_module, "get_db_connection", lambda: connection)

    assert StrategyCommandRepository().revoke_strategy_lease(strategy_id=11) is True

    sql, params = connection.cursor_value.executions[0]
    assert "UPDATE qd_strategy_runtime_leases" in sql
    assert "fencing_token = fencing_token + 1" in sql
    assert "owner_id = ''" in sql
    assert "owner_id = %s" not in sql
    assert params == (11,)


def test_stop_command_can_be_claimed_across_runtime_owners(monkeypatch):
    connection = FakeConnection(None)
    connection.commit = lambda: None
    connection.rollback = lambda: None
    monkeypatch.setattr(repository_module, "get_db_connection", lambda: connection)

    assert StrategyCommandRepository().claim_next(
        owner_id="worker-a",
        lease_seconds=30,
        max_attempts=3,
    ) is None

    sql, _params = connection.cursor_value.executions[0]
    assert "command.command_type IN ('start', 'stop', 'reconcile')" in sql


def test_releasing_shard_lease_preserves_fencing_row(monkeypatch):
    connection = FakeConnection(None)
    connection.commit = lambda: None
    connection.rollback = lambda: None
    monkeypatch.setattr(event_inbox_module, "get_db_connection", lambda: connection)

    assert StrategyShardLeaseRepository().release(
        strategy_shard=7,
        owner_id="worker-a",
        fencing_token=9,
    ) is True

    sql, params = connection.cursor_value.executions[0]
    assert "UPDATE qd_strategy_shard_leases" in sql
    assert "owner_id = ''" in sql
    assert "DELETE FROM qd_strategy_shard_leases" not in sql
    assert params == (7, "worker-a", 9)
