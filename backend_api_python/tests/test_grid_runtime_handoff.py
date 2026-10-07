from __future__ import annotations

from app.services.grid import runner as runner_module
from app.services.grid.runner import GridRestingRunner


def test_grid_runtime_detach_keeps_exchange_engine_untouched(monkeypatch):
    unregistered = []
    monkeypatch.setattr(
        runner_module,
        "unregister_runner",
        lambda strategy_id: unregistered.append(int(strategy_id)),
    )
    monkeypatch.setattr(
        GridRestingRunner,
        "checkpoint",
        lambda _self, **_kwargs: True,
    )
    runner = object.__new__(GridRestingRunner)
    runner.strategy_id = 71
    runner._started = True
    runner._engine = type("Engine", (), {
        "shutdown": lambda _self, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exchange orders must remain active")
        )
    })()

    runner.detach()

    assert runner._started is False
    assert unregistered == [71]
