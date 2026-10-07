from __future__ import annotations

import threading

from app.services import distributed_runtime_host as host_module
from app.services.distributed_runtime_host import DistributedRuntimeHost


class FakeHeartbeat:
    def __init__(self, *_args, **_kwargs):
        self.watched = set()

    def start(self):
        return None

    def watch_strategy(self, strategy_id):
        self.watched.add(int(strategy_id))

    def forget_strategy(self, strategy_id):
        self.watched.discard(int(strategy_id))

    def strategy_valid(self, strategy_id):
        return int(strategy_id) in self.watched

    def close(self):
        return None


class FakeRepository:
    def __init__(self):
        self.released = []

    def acquire_strategy_lease(self, **_kwargs):
        return 9

    def release_strategy_lease(self, *, strategy_id, owner_id):
        self.released.append((int(strategy_id), owner_id))


class RejectingExecutor:
    def __init__(self):
        self.lock = threading.Lock()
        self.runtime_guard = None
        self.running_strategies = {}
        self._last_start_failure = "Bitget position mode does not match the strategy."
        self.cleared = []

    def _discard_dead_runtimes(self):
        return None

    def is_running(self, _strategy_id):
        return False

    def set_runtime_fencing_token(self, _strategy_id, _token):
        return None

    def clear_runtime_fencing_token(self, strategy_id):
        self.cleared.append(int(strategy_id))

    def start_strategy(self, _strategy_id):
        return False


def test_distributed_start_failure_is_logged_once_with_exact_detail(monkeypatch):
    logs = []
    repository = FakeRepository()
    monkeypatch.setattr(host_module, "LeaseHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(
        host_module,
        "append_strategy_log",
        lambda strategy_id, level, message: logs.append((strategy_id, level, message)),
    )
    host = DistributedRuntimeHost(
        owner_id="evaluator-a",
        executor=RejectingExecutor(),
        repository=repository,
    )

    assert host.evaluate(55, object()) is False
    assert host.evaluate(55, object()) is False

    assert logs == [
        (55, "error", "Bitget position mode does not match the strategy."),
    ]
    assert repository.released == [(55, "evaluator-a"), (55, "evaluator-a")]
