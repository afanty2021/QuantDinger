from __future__ import annotations

import threading

from app.services.trading_executor import TradingExecutor


def test_stop_strategy_waits_until_the_runtime_thread_has_exited(monkeypatch):
    monkeypatch.setattr("app.services.trading_executor.append_strategy_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.services.grid.runner.shutdown_grid_for_strategy",
        lambda _strategy_id: None,
    )
    executor = TradingExecutor()
    stopped = threading.Event()
    finished = threading.Event()

    def runtime():
        stopped.wait(2)
        finished.set()

    thread = threading.Thread(target=runtime, daemon=True)
    executor.running_strategies[17] = thread
    executor._runtime_stop_events[17] = stopped
    thread.start()

    assert executor.stop_strategy(17, persist_status=False) is True
    assert finished.is_set()
    assert thread.is_alive() is False
    assert 17 not in executor.running_strategies
    assert 17 not in executor._runtime_stop_events


def test_stop_timeout_keeps_runtime_registered_and_blocks_restart(monkeypatch):
    monkeypatch.setattr("app.services.trading_executor.append_strategy_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.services.grid.runner.shutdown_grid_for_strategy",
        lambda _strategy_id: None,
    )
    executor = TradingExecutor()
    executor.stop_join_timeout = 0.01
    stop_event = threading.Event()
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    executor.running_strategies[18] = thread
    executor._runtime_stop_events[18] = stop_event
    thread.start()

    try:
        assert executor.stop_strategy(18, persist_status=False) is False
        assert stop_event.is_set()
        assert executor.running_strategies[18] is thread
        assert executor.start_strategy(18) is False
        assert executor._last_start_failure == "Strategy is already running."
    finally:
        release.set()
        thread.join(1)
