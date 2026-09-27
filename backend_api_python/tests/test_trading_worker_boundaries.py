"""Trading worker ownership and command boundary tests."""

from __future__ import annotations

import pytest

from app.services.strategy_command_repository import StrategyCommand
from app.workers.trading import TradingWorker


class FakeExecutor:
    def __init__(self, *, stop_result: bool = True) -> None:
        self.running_strategies = {}
        self.lock = __import__("threading").Lock()
        self.stopped = []
        self.stop_result = stop_result

    def stop_strategy(self, strategy_id, persist_status=False):
        del persist_status
        self.stopped.append(int(strategy_id))
        if self.stop_result:
            self.running_strategies.pop(int(strategy_id), None)
        return self.stop_result

    def stop_strategy_with_policy(self, strategy_id, *, close_positions):
        del close_positions
        stopped = self.stop_strategy(strategy_id)
        return {
            "strategy_id": strategy_id,
            "success": stopped,
            "message": "runtime stop timeout" if not stopped else "",
        }


class FakeRepository:
    def __init__(self) -> None:
        self.completed = []
        self.failed = []
        self.released = []

    def complete(self, command_id, result=None):
        self.completed.append((command_id, result))

    def fail(self, command_id, error, retry_delay_seconds=None):
        self.failed.append((command_id, error, retry_delay_seconds))

    def release_strategy_lease(self, *, strategy_id, owner_id):
        self.released.append((strategy_id, owner_id))


def _command(command_type: str) -> StrategyCommand:
    return StrategyCommand(
        id=1,
        strategy_id=55,
        user_id=1,
        command_type=command_type,
        status="processing",
        idempotency_key="test-command",
        payload={},
        attempts=1,
    )


def test_stop_command_is_executed_by_trading_worker():
    repository = FakeRepository()
    executor = FakeExecutor()
    worker = TradingWorker(executor, repository)

    worker._execute(_command("stop"))

    assert executor.stopped == [55]
    assert repository.released == [(55, worker.worker_id)]
    assert repository.completed[0][1]["status"] == "stopped"


def test_failed_command_is_retried_with_backoff(monkeypatch):
    repository = FakeRepository()
    worker = TradingWorker(FakeExecutor(), repository)
    monkeypatch.setattr(worker, "_start", lambda _strategy_id: (_ for _ in ()).throw(RuntimeError("boom")))

    worker._execute(_command("start"))

    assert repository.failed == [(1, "boom", 1)]


@pytest.mark.parametrize("close_positions", [False, True])
def test_stop_timeout_retains_runtime_lease(monkeypatch, close_positions):
    repository = FakeRepository()
    executor = FakeExecutor(stop_result=False)
    executor.running_strategies[55] = object()
    worker = TradingWorker(executor, repository)
    monkeypatch.setattr("app.workers.trading.append_strategy_log", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="stop"):
        worker._stop_strategy(55, close_positions=close_positions)

    assert repository.released == []
    assert 55 in executor.running_strategies


def test_worker_shutdown_retains_lease_when_runtime_does_not_stop(monkeypatch):
    repository = FakeRepository()
    executor = FakeExecutor(stop_result=False)
    executor.running_strategies[55] = object()
    worker = TradingWorker(executor, repository)
    monkeypatch.setattr("app.workers.trading.append_strategy_log", lambda *_args, **_kwargs: None)

    worker._shutdown_local_runtimes()

    assert repository.released == []
    assert 55 in executor.running_strategies
