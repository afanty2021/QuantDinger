from __future__ import annotations

import threading
import time

from app.services.strategy_runtime.scheduler import CooperativeRuntimeScheduler


def test_scheduler_runs_many_runtimes_on_a_bounded_pool():
    scheduler = CooperativeRuntimeScheduler(worker_count=3)
    lock = threading.Lock()
    worker_names = set()
    completed = []

    def runtime(strategy_id):
        for _index in range(3):
            with lock:
                worker_names.add(threading.current_thread().name)
            yield 0.001
        completed.append(strategy_id)

    handles = []
    for strategy_id in range(100):
        stop_event = threading.Event()
        handle = scheduler.create(
            strategy_id=strategy_id,
            runtime=runtime(strategy_id),
            stop_event=stop_event,
        )
        handles.append(handle)
        handle.start()

    for handle in handles:
        handle.join(3)

    scheduler.close()
    assert sorted(completed) == list(range(100))
    assert 1 <= len(worker_names) <= 3
    assert all(name.startswith("strategy-evaluator-") for name in worker_names)


def test_scheduler_cancel_wakes_and_finalizes_a_sleeping_runtime():
    scheduler = CooperativeRuntimeScheduler(worker_count=1)
    entered = threading.Event()
    finalized = threading.Event()
    stop_event = threading.Event()

    def runtime():
        try:
            while not stop_event.is_set():
                entered.set()
                yield 60.0
        finally:
            finalized.set()

    handle = scheduler.create(
        strategy_id=7,
        runtime=runtime(),
        stop_event=stop_event,
    )
    handle.start()
    assert entered.wait(1)

    handle.cancel()
    handle.join(1)

    scheduler.close()
    assert not handle.is_alive()
    assert finalized.is_set()


def test_scheduler_never_runs_one_runtime_concurrently():
    scheduler = CooperativeRuntimeScheduler(worker_count=4)
    stop_event = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def runtime():
        nonlocal active, peak
        for _index in range(20):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.002)
            with lock:
                active -= 1
            yield 0.0

    handle = scheduler.create(
        strategy_id=8,
        runtime=runtime(),
        stop_event=stop_event,
    )
    handle.start()
    for _index in range(20):
        handle._wake(handle, 0.0)
    handle.join(2)

    scheduler.close()
    assert peak == 1


def test_scheduler_event_wake_interrupts_sleep_and_coalesces_burst():
    scheduler = CooperativeRuntimeScheduler(worker_count=1)
    stop_event = threading.Event()
    first_cycle = threading.Event()
    second_cycle = threading.Event()
    release_first = threading.Event()
    cycles = 0

    def runtime():
        nonlocal cycles
        while not stop_event.is_set():
            cycles += 1
            if cycles == 1:
                first_cycle.set()
                release_first.wait(1)
            if cycles == 2:
                second_cycle.set()
            yield 60.0

    handle = scheduler.create(
        strategy_id=81,
        runtime=runtime(),
        stop_event=stop_event,
    )
    handle.start()
    assert first_cycle.wait(1)

    for sequence in range(20):
        assert scheduler.wake_strategy(81, {"sequence": sequence})
    release_first.set()
    assert second_cycle.wait(1)
    time.sleep(0.03)

    snapshot = scheduler.snapshot()
    handle.cancel()
    handle.join(1)
    scheduler.close()

    assert cycles == 2
    assert snapshot["event_wakes"] == 20


def test_scheduler_can_acknowledge_an_externally_triggered_cycle():
    scheduler = CooperativeRuntimeScheduler(worker_count=1)
    stop_event = threading.Event()
    cycles = 0

    def runtime():
        nonlocal cycles
        while not stop_event.is_set():
            cycles += 1
            yield 60.0

    handle = scheduler.create(
        strategy_id=91,
        runtime=runtime(),
        stop_event=stop_event,
    )
    handle.start()
    assert handle.wait_ready(1)

    assert scheduler.wake_strategy_and_wait(91, {"bar": 123}, timeout=1)
    assert cycles == 2
    assert handle.completed_steps() == 2

    handle.cancel()
    handle.join(1)
    scheduler.close()


def test_scheduler_close_discards_stale_future_entries_immediately():
    scheduler = CooperativeRuntimeScheduler(worker_count=4)
    handles = []

    def runtime(stop_event):
        while not stop_event.is_set():
            yield 3600.0

    for strategy_id in range(200):
        stop_event = threading.Event()
        handle = scheduler.create(
            strategy_id=strategy_id,
            runtime=runtime(stop_event),
            stop_event=stop_event,
        )
        handles.append(handle)
        handle.start()
    assert all(handle.wait_ready(2) for handle in handles)

    started_at = time.monotonic()
    scheduler.close(timeout=2)

    assert time.monotonic() - started_at < 1.0
    assert all(not handle.is_alive() for handle in handles)
