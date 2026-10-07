"""Fixed-size cooperative scheduler for long-lived strategy runtimes."""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Generator
from typing import Any, Callable

from app.utils.logger import get_logger


logger = get_logger(__name__)


class ScheduledRuntimeHandle:
    """Thread-compatible lifecycle handle for one scheduled runtime."""

    def __init__(
        self,
        *,
        strategy_id: int,
        runtime: Generator[float, None, None],
        stop_event: threading.Event,
        wake: Callable[["ScheduledRuntimeHandle", float], None],
    ) -> None:
        self.strategy_id = int(strategy_id)
        self.runtime = runtime
        self.stop_event = stop_event
        self._wake = wake
        self._done = threading.Event()
        self._ready = threading.Event()
        self._started = False
        self._schedule_version = 0
        self._run_lock = threading.Lock()
        self._running = False
        self._event_pending = False
        self._event_wake_queued = False
        self._last_event: Any = None
        self._step_condition = threading.Condition()
        self._completed_steps = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._wake(self, 0.0)

    def cancel(self) -> None:
        self.stop_event.set()
        if self._started and not self._done.is_set():
            self._wake(self, 0.0)

    def join(self, timeout: float | None = None) -> None:
        self._done.wait(timeout)

    def is_alive(self) -> bool:
        return self._started and not self._done.is_set()

    def is_ready(self) -> bool:
        return self._ready.is_set() and not self._done.is_set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        self._ready.wait(timeout)
        return self.is_ready()

    def _mark_ready(self) -> None:
        self._ready.set()

    def _mark_done(self) -> None:
        self._done.set()
        with self._step_condition:
            self._step_condition.notify_all()

    def completed_steps(self) -> int:
        with self._step_condition:
            return self._completed_steps

    def wait_for_step(self, target_step: int, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._step_condition:
            while self._completed_steps < int(target_step) and not self._done.is_set():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._step_condition.wait(remaining)
            return self._completed_steps >= int(target_step)

    def _mark_step_complete(self) -> None:
        with self._step_condition:
            self._completed_steps += 1
            self._step_condition.notify_all()


class CooperativeRuntimeScheduler:
    """Advance many yielding strategy runtimes on a bounded worker pool."""

    def __init__(self, *, worker_count: int) -> None:
        self.worker_count = max(1, int(worker_count))
        self._condition = threading.Condition()
        self._queue: list[tuple[float, int, int, bool, ScheduledRuntimeHandle]] = []
        self._sequence = itertools.count()
        self._workers: list[threading.Thread] = []
        self._handles: set[ScheduledRuntimeHandle] = set()
        self._handles_by_strategy: dict[int, ScheduledRuntimeHandle] = {}
        self._event_wake_count = 0
        self._queue_compactions = 0
        self._closed = False

    def create(
        self,
        *,
        strategy_id: int,
        runtime: Generator[float, None, None],
        stop_event: threading.Event,
    ) -> ScheduledRuntimeHandle:
        handle = ScheduledRuntimeHandle(
            strategy_id=strategy_id,
            runtime=runtime,
            stop_event=stop_event,
            wake=self._schedule,
        )
        with self._condition:
            if self._closed:
                raise RuntimeError("strategyRuntime.schedulerClosed")
            self._handles.add(handle)
            self._handles_by_strategy[handle.strategy_id] = handle
        return handle

    def wake_strategy(self, strategy_id: int, event: Any = None) -> bool:
        """Wake one sleeping runtime and coalesce bursts arriving during evaluation."""
        with self._condition:
            handle = self._handles_by_strategy.get(int(strategy_id))
            if handle is None or not handle.is_alive() or self._closed:
                return False
            handle._last_event = event
            handle._event_pending = True
            self._event_wake_count += 1
            if handle._running or handle._event_wake_queued:
                return True
            handle._event_wake_queued = True
            self._schedule_locked(handle, 0.0, event_wake=True)
            return True

    def wake_strategy_and_wait(
        self,
        strategy_id: int,
        event: Any = None,
        *,
        timeout: float = 30.0,
    ) -> bool:
        with self._condition:
            handle = self._handles_by_strategy.get(int(strategy_id))
            if handle is None or not handle.is_alive() or self._closed:
                return False
            completed = handle.completed_steps()
            target_step = completed + (2 if handle._running else 1)
            handle._last_event = event
            handle._event_pending = True
            self._event_wake_count += 1
            if not handle._running and not handle._event_wake_queued:
                handle._event_wake_queued = True
                self._schedule_locked(handle, 0.0, event_wake=True)
        return handle.wait_for_step(target_step, timeout=max(0.0, float(timeout)))

    def start(self) -> None:
        with self._condition:
            if self._workers:
                return
            if self._closed:
                raise RuntimeError("strategyRuntime.schedulerClosed")
            for index in range(self.worker_count):
                worker = threading.Thread(
                    target=self._run,
                    name=f"strategy-evaluator-{index + 1}",
                    daemon=True,
                )
                self._workers.append(worker)
                worker.start()

    def close(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._closed = True
            handles = set(self._handles)
            self._condition.notify_all()
        for handle in handles:
            handle.cancel()
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "workers": len(self._workers),
                "queued_runtimes": sum(handle.is_alive() for handle in self._handles),
                "scheduled_entries": len(self._queue),
                "event_wakes": self._event_wake_count,
                "queue_compactions": self._queue_compactions,
            }

    def _forget(self, handle: ScheduledRuntimeHandle) -> None:
        with self._condition:
            self._handles.discard(handle)
            if self._handles_by_strategy.get(handle.strategy_id) is handle:
                self._handles_by_strategy.pop(handle.strategy_id, None)
            self._condition.notify_all()

    def _schedule(self, handle: ScheduledRuntimeHandle, delay_seconds: float) -> None:
        with self._condition:
            if self._closed:
                if not handle._started:
                    handle._mark_done()
                    self._handles.discard(handle)
                    return
            self.start()
            self._schedule_locked(handle, delay_seconds)

    def _schedule_locked(
        self,
        handle: ScheduledRuntimeHandle,
        delay_seconds: float,
        *,
        event_wake: bool = False,
    ) -> None:
        handle._schedule_version += 1
        version = handle._schedule_version
        due_at = time.monotonic() + max(0.0, float(delay_seconds or 0.0))
        heapq.heappush(
            self._queue,
            (due_at, next(self._sequence), version, event_wake, handle),
        )
        self._compact_queue_locked()
        self._condition.notify()

    def _compact_queue_locked(self) -> None:
        if len(self._queue) <= max(128, len(self._handles) * 2):
            return
        self._queue = [
            entry
            for entry in self._queue
            if entry[2] == entry[4]._schedule_version and entry[4].is_alive()
        ]
        heapq.heapify(self._queue)
        self._queue_compactions += 1

    def _run(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._closed and not self._queue:
                        return
                    if not self._queue:
                        self._condition.wait()
                        continue
                    due_at, _sequence, version, event_wake, handle = self._queue[0]
                    if version != handle._schedule_version or not handle.is_alive():
                        heapq.heappop(self._queue)
                        continue
                    remaining = due_at - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    heapq.heappop(self._queue)
                    if event_wake:
                        handle._event_wake_queued = False
                    handle._event_pending = False
                    handle._running = True
                    break

            with handle._run_lock:
                if (
                    not handle.is_alive()
                    or version != handle._schedule_version
                ):
                    with self._condition:
                        handle._running = False
                    continue
                try:
                    delay = next(handle.runtime)
                except StopIteration:
                    with self._condition:
                        handle._running = False
                    handle._mark_done()
                    self._forget(handle)
                    continue
                except Exception:
                    logger.exception(
                        "Scheduled strategy runtime failed: strategy=%s",
                        handle.strategy_id,
                    )
                    with self._condition:
                        handle._running = False
                    handle._mark_done()
                    self._forget(handle)
                    continue
            handle._mark_step_complete()
            handle._mark_ready()
            with self._condition:
                handle._running = False
                event_pending = handle._event_pending
                if event_pending:
                    handle._event_wake_queued = True
                next_delay = (
                    0.0
                    if handle.stop_event.is_set() or event_pending
                    else max(0.0, float(delay or 0.0))
                )
                self._schedule_locked(
                    handle,
                    next_delay,
                    event_wake=event_pending,
                )


__all__ = ["CooperativeRuntimeScheduler", "ScheduledRuntimeHandle"]
