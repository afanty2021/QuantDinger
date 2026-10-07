"""Active evaluator worker with durable inbox and shard fencing."""

from __future__ import annotations

import os
import time


def main() -> None:
    os.environ["QD_PROCESS_ROLE"] = "strategy-evaluator"

    from app import create_app
    from app.events.evaluator import StrategyEvaluationBatchHandler
    from app.events.kafka import KafkaEventConsumer
    from app.events.protocol import StrategyEvaluationBatchV1
    from app.events.topics import topic_for_event
    from app.runtime.process import ShutdownSignal
    from app.services.strategy_command_repository import StrategyCommandRepository
    from app.utils.logger import get_logger
    from app.workers.trading import build_worker_id

    logger = get_logger(__name__)
    app = create_app(register_http_routes=False)
    shutdown = ShutdownSignal()
    shutdown.install()
    heartbeat_repository = StrategyCommandRepository()
    worker_id = build_worker_id()
    group_id = os.getenv(
        "KAFKA_STRATEGY_EVALUATOR_GROUP_ID",
        "quantdinger-strategy-evaluator-active-v1",
    )
    handler = StrategyEvaluationBatchHandler(
        owner_id=worker_id,
        consumer_group=group_id,
    )
    consumer = KafkaEventConsumer(
        topics=[topic_for_event(StrategyEvaluationBatchV1.event_type)],
        group_id=group_id,
        on_partitions_revoked=handler.release_partition_keys,
    )
    last_heartbeat = 0.0

    with app.app_context():
        try:
            logger.info(
                "Strategy evaluator worker started: worker=%s mode=%s",
                worker_id,
                handler.mode,
            )
            while not shutdown.event.is_set():
                consumer.poll_once(handler.handle, timeout=1.0)
                now = time.monotonic()
                if now - last_heartbeat >= 10.0:
                    if handler.runtime_host is not None:
                        local_ids = handler.runtime_host.local_strategy_ids()
                        running_ids = set(
                            handler.subscriptions.running_strategy_ids(local_ids)
                        )
                        handler.runtime_host.reconcile(running_ids)
                    heartbeat_repository.record_worker_heartbeat(
                        worker_id=worker_id,
                        role="strategy-evaluator",
                        metadata={**consumer.snapshot(), **handler.snapshot()},
                    )
                    last_heartbeat = now
        finally:
            consumer.close()
            handler.close()
            heartbeat_repository.mark_worker_stopped(worker_id)
            logger.info("Strategy evaluator worker stopped: %s", worker_id)


if __name__ == "__main__":
    main()
