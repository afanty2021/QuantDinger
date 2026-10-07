"""Kafka worker that maps market events to strategy-shard evaluation batches."""

from __future__ import annotations

import os
import time


def main() -> None:
    os.environ["QD_PROCESS_ROLE"] = "strategy-dispatcher"

    from app import create_app
    from app.events.dispatcher import StrategyDueDispatcher
    from app.events.kafka import KafkaEventConsumer
    from app.events.protocol import MarketBarClosedV1
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
    dispatcher = StrategyDueDispatcher()
    consumer = KafkaEventConsumer(
        topics=[topic_for_event(MarketBarClosedV1.event_type)],
        group_id=os.getenv(
            "KAFKA_STRATEGY_DISPATCH_GROUP_ID",
            "quantdinger-strategy-dispatch-v1",
        ),
    )
    last_heartbeat = 0.0

    with app.app_context():
        try:
            logger.info("Strategy dispatcher worker started: %s", worker_id)
            while not shutdown.event.is_set():
                consumer.poll_once(dispatcher.dispatch, timeout=1.0)
                now = time.monotonic()
                if now - last_heartbeat >= 10.0:
                    heartbeat_repository.record_worker_heartbeat(
                        worker_id=worker_id,
                        role="strategy-dispatcher",
                        metadata={**consumer.snapshot(), **dispatcher.snapshot()},
                    )
                    last_heartbeat = now
        finally:
            consumer.close()
            dispatcher.close()
            heartbeat_repository.mark_worker_stopped(worker_id)
            logger.info("Strategy dispatcher worker stopped: %s", worker_id)


if __name__ == "__main__":
    main()
