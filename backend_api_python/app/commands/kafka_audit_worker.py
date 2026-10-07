"""Independent consumer that validates active Kafka runtime events."""

from __future__ import annotations

import os
import time


def main() -> None:
    os.environ["QD_PROCESS_ROLE"] = "kafka-audit"

    from app import create_app
    from app.events.kafka import KafkaEventConsumer
    from app.events.protocol import (
        EventEnvelope,
        MarketBarClosedV1,
        StrategyEvaluationBatchV1,
    )
    from app.events.topics import topic_for_event
    from app.runtime.process import ShutdownSignal
    from app.services.strategy_command_repository import StrategyCommandRepository
    from app.utils.logger import get_logger
    from app.workers.trading import build_worker_id

    logger = get_logger(__name__)
    app = create_app(register_http_routes=False)
    shutdown = ShutdownSignal()
    shutdown.install()
    repository = StrategyCommandRepository()
    worker_id = build_worker_id()
    consumer = KafkaEventConsumer(
        topics=[
            topic_for_event(MarketBarClosedV1.event_type),
            topic_for_event(StrategyEvaluationBatchV1.event_type),
        ],
        group_id=os.getenv("KAFKA_AUDIT_GROUP_ID", "quantdinger-runtime-audit-v1"),
    )
    last_event: dict[str, object] = {}
    last_heartbeat = 0.0

    def handle(event: EventEnvelope) -> bool:
        payload = event.payload
        if event.event_type == MarketBarClosedV1.event_type:
            required = (
                "venue",
                "market_type",
                "instrument_id",
                "timeframe",
                "closed_bar_token",
            )
        elif event.event_type == StrategyEvaluationBatchV1.event_type:
            required = (
                "strategy_shard",
                "strategy_ids",
                "source_event_id",
                "timeframe",
                "closed_bar_token",
            )
            if not isinstance(payload.get("strategy_ids"), list) or not payload["strategy_ids"]:
                raise ValueError(f"kafkaAudit.invalidStrategyBatch:{event.event_id}")
        else:
            logger.warning("Unexpected runtime event type: %s", event.event_type)
            return True
        if any(payload.get(field) in (None, "") for field in required):
            raise ValueError(f"kafkaAudit.invalidEvent:{event.event_id}")
        last_event.clear()
        last_event.update({
            "last_event_id": event.event_id,
            "last_event_type": event.event_type,
            "last_partition_key": event.partition_key,
            "last_occurred_at": event.occurred_at,
        })
        return True

    with app.app_context():
        try:
            logger.info("Kafka audit worker started: %s", worker_id)
            while not shutdown.event.is_set():
                consumer.poll_once(handle, timeout=1.0)
                now = time.monotonic()
                if now - last_heartbeat >= 10.0:
                    repository.record_worker_heartbeat(
                        worker_id=worker_id,
                        role="kafka-audit",
                        metadata={**consumer.snapshot(), **last_event},
                    )
                    last_heartbeat = now
        finally:
            consumer.close()
            repository.mark_worker_stopped(worker_id)
            logger.info("Kafka audit worker stopped: %s", worker_id)


if __name__ == "__main__":
    main()
