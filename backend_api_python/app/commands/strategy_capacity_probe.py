"""Safe in-process capacity probe for the strategy scheduling pipeline."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import tracemalloc
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from app.events.dispatcher import StrategyDueDispatcher
from app.events.evaluator import StrategyEvaluationBatchHandler
from app.events.protocol import MarketBarClosedV1, StrategyEvaluationBatchV1
from app.services.event_inbox import InboxClaim
from app.services.strategy_event_subscriptions import strategy_shard
from app.services.strategy_runtime.scheduler import CooperativeRuntimeScheduler


@dataclass(frozen=True, slots=True)
class ProbeStage:
    name: str
    strategies: int
    operations: int
    elapsed_ms: float
    operations_per_second: float


class _RoutingRepository:
    def __init__(self, strategy_ids: list[int]) -> None:
        self.strategy_ids = strategy_ids

    def strategy_ids_for_event(self, _event_type: str, _partition_key: str) -> list[int]:
        return list(self.strategy_ids)


class _SerializingProducer:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.serialized_bytes = 0

    def publish_batch(self, events: list[Any], *, timeout: float) -> bool:
        del timeout
        self.events.extend(events)
        self.serialized_bytes += sum(len(event.to_json().encode("utf-8")) for event in events)
        return True

    def snapshot(self) -> dict[str, int]:
        return {
            "probe_events": len(self.events),
            "probe_serialized_bytes": self.serialized_bytes,
        }

    def close(self) -> None:
        return None


class _Inbox:
    def claim(self, **_kwargs: Any) -> InboxClaim:
        return InboxClaim("acquired", 1)

    def complete(self, **_kwargs: Any) -> bool:
        return True

    def fail(self, **_kwargs: Any) -> bool:
        return True


class _ShardLeases:
    def __init__(self) -> None:
        self.token = 0

    def acquire(self, **_kwargs: Any) -> int:
        self.token += 1
        return self.token

    def release(self, **_kwargs: Any) -> bool:
        return True

    def release_all(self, **_kwargs: Any) -> int:
        return 0


class _Subscriptions:
    @staticmethod
    def running_strategy_ids(strategy_ids: list[int]) -> list[int]:
        return list(strategy_ids)


class _RuntimeHost:
    def __init__(self) -> None:
        self.evaluations = 0

    def evaluate(self, _strategy_id: int, _event: Any, *, timeout: float) -> bool:
        del timeout
        self.evaluations += 1
        return True

    def release_strategies(self, strategy_ids: set[int]) -> int:
        return len(strategy_ids)

    def snapshot(self) -> dict[str, int]:
        return {"probe_runtime_evaluations": self.evaluations}

    def close(self) -> None:
        return None


def _stage(name: str, strategies: int, operations: int, elapsed: float) -> ProbeStage:
    return ProbeStage(
        name=name,
        strategies=strategies,
        operations=operations,
        elapsed_ms=round(elapsed * 1000.0, 3),
        operations_per_second=round(operations / max(elapsed, 1e-9), 3),
    )


def _runtime(stop_event: threading.Event):
    while not stop_event.is_set():
        yield 3600.0


def probe_scheduler(strategy_count: int, worker_count: int, timeout: float) -> list[ProbeStage]:
    scheduler = CooperativeRuntimeScheduler(worker_count=worker_count)
    handles = []
    started_at = time.perf_counter()
    for strategy_id in range(1, strategy_count + 1):
        stop_event = threading.Event()
        handle = scheduler.create(
            strategy_id=strategy_id,
            runtime=_runtime(stop_event),
            stop_event=stop_event,
        )
        handles.append(handle)
        handle.start()
    deadline = time.monotonic() + timeout
    for handle in handles:
        if not handle.wait_ready(max(0.0, deadline - time.monotonic())):
            scheduler.close(timeout=5.0)
            raise TimeoutError("capacityProbe.schedulerStartupTimeout")
    startup_elapsed = time.perf_counter() - started_at

    targets = {handle.strategy_id: handle.completed_steps() + 1 for handle in handles}
    wake_started_at = time.perf_counter()
    for handle in handles:
        scheduler.wake_strategy(handle.strategy_id)
    deadline = time.monotonic() + timeout
    for handle in handles:
        if not handle.wait_for_step(
            targets[handle.strategy_id],
            max(0.0, deadline - time.monotonic()),
        ):
            scheduler.close(timeout=5.0)
            raise TimeoutError("capacityProbe.schedulerWakeTimeout")
    wake_elapsed = time.perf_counter() - wake_started_at
    scheduler.close(timeout=min(15.0, timeout))
    return [
        _stage("scheduler_start", strategy_count, strategy_count, startup_elapsed),
        _stage("scheduler_wake", strategy_count, strategy_count, wake_elapsed),
    ]


def probe_dispatcher(
    strategy_count: int,
    shard_count: int,
    batch_size: int,
) -> tuple[ProbeStage, list[Any], int]:
    os.environ["STRATEGY_SHARD_COUNT"] = str(shard_count)
    strategy_ids = list(range(1, strategy_count + 1))
    producer = _SerializingProducer()
    dispatcher = StrategyDueDispatcher(
        repository=_RoutingRepository(strategy_ids),
        producer=producer,
        batch_size=batch_size,
    )
    event = MarketBarClosedV1.create(
        venue="capacity-probe",
        market="Crypto",
        market_type="swap",
        instrument_id="BTCUSDT",
        symbol="BTC/USDT",
        timeframe="1m",
        closed_bar_token=1,
        closed_at=datetime.now(timezone.utc),
    )
    started_at = time.perf_counter()
    if not dispatcher.dispatch(event):
        raise RuntimeError("capacityProbe.dispatchFailed")
    elapsed = time.perf_counter() - started_at
    return (
        _stage("event_dispatch", strategy_count, strategy_count, elapsed),
        list(producer.events),
        producer.serialized_bytes,
    )


def probe_evaluator(events: list[Any], strategy_count: int, worker_count: int) -> ProbeStage:
    runtime_host = _RuntimeHost()
    previous = os.environ.get("STRATEGY_EVALUATOR_BATCH_WORKERS")
    os.environ["STRATEGY_EVALUATOR_BATCH_WORKERS"] = str(worker_count)
    handler = StrategyEvaluationBatchHandler(
        owner_id="capacity-probe",
        consumer_group="capacity-probe",
        inbox=_Inbox(),
        shard_leases=_ShardLeases(),
        subscriptions=_Subscriptions(),
        mode="active",
        runtime_host=runtime_host,
    )
    try:
        started_at = time.perf_counter()
        for event in events:
            if not handler.handle(event):
                raise RuntimeError("capacityProbe.evaluationFailed")
        elapsed = time.perf_counter() - started_at
    finally:
        handler.close()
        if previous is None:
            os.environ.pop("STRATEGY_EVALUATOR_BATCH_WORKERS", None)
        else:
            os.environ["STRATEGY_EVALUATOR_BATCH_WORKERS"] = previous
    if runtime_host.evaluations != strategy_count:
        raise RuntimeError("capacityProbe.evaluationCountMismatch")
    return _stage("evaluator_fanout", strategy_count, runtime_host.evaluations, elapsed)


def run_probe(
    *,
    strategy_count: int = 5_000,
    shard_count: int = 128,
    batch_size: int = 250,
    worker_count: int = 16,
    timeout: float = 60.0,
) -> dict[str, Any]:
    strategy_count = max(1, int(strategy_count))
    shard_count = max(1, int(shard_count))
    batch_size = max(1, int(batch_size))
    worker_count = max(1, int(worker_count))
    tracemalloc.start()
    baseline_current, _baseline_peak = tracemalloc.get_traced_memory()
    started_at = time.perf_counter()
    stages = probe_scheduler(strategy_count, worker_count, timeout)
    dispatch_stage, events, serialized_bytes = probe_dispatcher(
        strategy_count,
        shard_count,
        batch_size,
    )
    stages.append(dispatch_stage)
    stages.append(probe_evaluator(events, strategy_count, worker_count))
    total_elapsed = time.perf_counter() - started_at
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    grouped: dict[int, int] = defaultdict(int)
    for strategy_id in range(1, strategy_count + 1):
        grouped[strategy_shard(strategy_id, shard_count)] += 1
    return {
        "safe_mode": True,
        "external_io": False,
        "orders_submitted": 0,
        "strategies": strategy_count,
        "strategy_shards": shard_count,
        "evaluation_batches": len(events),
        "largest_shard": max(grouped.values(), default=0),
        "batch_size": batch_size,
        "worker_count": worker_count,
        "serialized_event_bytes": serialized_bytes,
        "python_heap_current_mb": round((current_bytes - baseline_current) / 1024 / 1024, 3),
        "python_heap_peak_mb": round((peak_bytes - baseline_current) / 1024 / 1024, 3),
        "total_elapsed_ms": round(total_elapsed * 1000.0, 3),
        "stages": [asdict(stage) for stage in stages],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", type=int, default=5_000)
    parser.add_argument("--shards", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    print(json.dumps(run_probe(
        strategy_count=args.strategies,
        shard_count=args.shards,
        batch_size=args.batch_size,
        worker_count=args.workers,
        timeout=args.timeout,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
