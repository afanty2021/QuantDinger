"""Kafka transport adapters for versioned runtime events."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from app.events.bus import EventHandler, EventSubscription, InMemoryEventBus
from app.events.protocol import EventEnvelope
from app.events.topics import topic_for_event
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _enabled(name: str, default: bool = False) -> bool:
    value = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def kafka_client_config(*, client_id: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092"),
        "client.id": client_id,
        "security.protocol": os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"),
    }
    username = str(os.getenv("KAFKA_SASL_USERNAME") or "").strip()
    password = str(os.getenv("KAFKA_SASL_PASSWORD") or "").strip()
    if username:
        config["sasl.mechanism"] = os.getenv("KAFKA_SASL_MECHANISM", "PLAIN")
        config["sasl.username"] = username
        config["sasl.password"] = password
    return config


class KafkaEventProducer:
    """Asynchronous idempotent Kafka producer keyed by envelope partition key."""

    def __init__(self, producer: Any | None = None) -> None:
        if producer is None:
            from confluent_kafka import Producer

            client_id = os.getenv("KAFKA_CLIENT_ID", "quantdinger-runtime")
            producer = Producer({
                **kafka_client_config(client_id=client_id),
                "acks": "all",
                "enable.idempotence": True,
                "compression.type": os.getenv("KAFKA_COMPRESSION_TYPE", "zstd"),
                "linger.ms": int(os.getenv("KAFKA_LINGER_MS", "5")),
                "delivery.timeout.ms": int(os.getenv("KAFKA_DELIVERY_TIMEOUT_MS", "30000")),
            })
        self._producer = producer
        self._lock = threading.Lock()
        self._queued = 0
        self._delivered = 0
        self._delivery_errors = 0

    def publish(self, event: EventEnvelope) -> None:
        def delivered(error: Any, _message: Any) -> None:
            with self._lock:
                if error is None:
                    self._delivered += 1
                else:
                    self._delivery_errors += 1
            if error is not None:
                logger.error(
                    "Kafka event delivery failed: type=%s event_id=%s error=%s",
                    event.event_type,
                    event.event_id,
                    error,
                )

        self._producer.produce(
            topic=topic_for_event(event.event_type),
            key=event.partition_key.encode("utf-8"),
            value=event.to_json().encode("utf-8"),
            headers={
                "event_type": event.event_type,
                "schema_version": str(event.schema_version),
                "event_id": event.event_id,
            },
            on_delivery=delivered,
        )
        self._producer.poll(0)
        with self._lock:
            self._queued += 1

    def flush(self, timeout: float = 5.0) -> int:
        return int(self._producer.flush(max(0.0, float(timeout))))

    def publish_batch(self, events: list[EventEnvelope], *, timeout: float = 10.0) -> bool:
        with self._lock:
            errors_before = self._delivery_errors
        for event in events:
            self.publish(event)
        outstanding = self.flush(timeout)
        with self._lock:
            delivery_failed = self._delivery_errors > errors_before
        return outstanding == 0 and not delivery_failed

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "kafka_events_queued": self._queued,
                "kafka_events_delivered": self._delivered,
                "kafka_delivery_errors": self._delivery_errors,
            }

    def close(self) -> None:
        outstanding = self.flush(float(os.getenv("KAFKA_FLUSH_TIMEOUT_SEC", "5")))
        if outstanding:
            logger.warning("Kafka producer closed with %s undelivered events", outstanding)


class MirroredEventBus:
    """Deliver locally first and mirror to Kafka without blocking live execution."""

    def __init__(
        self,
        local_bus: InMemoryEventBus | None = None,
        kafka_producer: KafkaEventProducer | None = None,
    ) -> None:
        self._local = local_bus or InMemoryEventBus()
        self._kafka = kafka_producer or KafkaEventProducer()
        self._lock = threading.Lock()
        self._publish_errors = 0

    def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
        *,
        partition_key: str = "*",
    ) -> EventSubscription:
        return self._local.subscribe(event_type, handler, partition_key=partition_key)

    def publish(self, event: EventEnvelope) -> int:
        delivered = self._local.publish(event)
        try:
            self._kafka.publish(event)
        except Exception as exc:
            with self._lock:
                self._publish_errors += 1
            logger.warning(
                "Kafka event publish failed: type=%s event_id=%s error=%s",
                event.event_type,
                event.event_id,
                exc,
            )
        return delivered

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            publish_errors = self._publish_errors
        return {
            **self._local.snapshot(),
            **self._kafka.snapshot(),
            "kafka_event_publish_errors": publish_errors,
        }

    def close(self) -> None:
        self._local.close()
        try:
            self._kafka.close()
        except Exception:
            logger.exception("Kafka producer close failed")


class KafkaEventConsumer:
    """Manual-commit consumer that advances offsets only after successful handling."""

    def __init__(
        self,
        *,
        topics: list[str],
        group_id: str,
        consumer: Any | None = None,
        on_partitions_revoked: Callable[[set[str]], None] | None = None,
    ) -> None:
        if consumer is None:
            from confluent_kafka import Consumer

            consumer = Consumer({
                **kafka_client_config(client_id=os.getenv("KAFKA_CONSUMER_CLIENT_ID", group_id)),
                "group.id": group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest"),
                "isolation.level": "read_committed",
                "partition.assignment.strategy": "cooperative-sticky",
                "max.poll.interval.ms": int(
                    os.getenv("KAFKA_MAX_POLL_INTERVAL_MS", "900000")
                ),
            })
        self._consumer = consumer
        self._on_partitions_revoked_callback = on_partitions_revoked
        self._partition_keys: dict[tuple[str, int], set[str]] = {}
        if on_partitions_revoked is None:
            self._consumer.subscribe(topics)
        else:
            self._consumer.subscribe(
                topics,
                on_revoke=self._on_revoke,
                on_lost=self._on_revoke,
            )
        self._received = 0
        self._handled = 0
        self._invalid = 0
        self._handler_errors = 0

    def poll_once(
        self,
        handler: Callable[[EventEnvelope], bool | None],
        *,
        timeout: float = 1.0,
    ) -> bool:
        message = self._consumer.poll(timeout=max(0.0, float(timeout)))
        if message is None:
            return False
        error = message.error()
        if error is not None:
            logger.warning("Kafka consumer poll error: %s", error)
            return False
        self._received += 1
        try:
            event = EventEnvelope.from_json(message.value())
        except Exception as exc:
            self._invalid += 1
            logger.error("Invalid Kafka event skipped: error=%s", exc)
            self._consumer.commit(message=message, asynchronous=False)
            return True
        partition = (str(message.topic()), int(message.partition()))
        self._partition_keys.setdefault(partition, set()).add(event.partition_key)
        try:
            handled = handler(event)
        except Exception:
            self._handler_errors += 1
            logger.exception("Kafka event handler failed: event_id=%s", event.event_id)
            self._rewind(message)
            return True
        if handled is False:
            self._rewind(message)
            return True
        self._consumer.commit(message=message, asynchronous=False)
        self._handled += 1
        return True

    def snapshot(self) -> dict[str, int]:
        return {
            "kafka_messages_received": self._received,
            "kafka_messages_handled": self._handled,
            "kafka_messages_invalid": self._invalid,
            "kafka_handler_errors": self._handler_errors,
            "kafka_tracked_partitions": len(self._partition_keys),
        }

    def close(self) -> None:
        self._consumer.close()

    def _rewind(self, message: Any) -> None:
        from confluent_kafka import TopicPartition

        self._consumer.seek(
            TopicPartition(message.topic(), message.partition(), message.offset())
        )

    def _on_revoke(self, _consumer: Any, partitions: list[Any]) -> None:
        partition_keys: set[str] = set()
        revoked: list[tuple[str, int]] = []
        for partition in partitions:
            identity = (str(partition.topic), int(partition.partition))
            revoked.append(identity)
            partition_keys.update(self._partition_keys.get(identity, set()))
        callback = self._on_partitions_revoked_callback
        if callback is not None and partition_keys:
            try:
                callback(partition_keys)
            except Exception:
                logger.exception(
                    "Kafka partition revoke callback failed: partitions=%s",
                    revoked,
                )
        for identity in revoked:
            self._partition_keys.pop(identity, None)


def build_runtime_event_bus() -> InMemoryEventBus | MirroredEventBus:
    local = InMemoryEventBus()
    if not _enabled("KAFKA_EVENT_PUBLISH_ENABLED", default=True):
        return local
    try:
        return MirroredEventBus(local_bus=local)
    except Exception as exc:
        logger.error("Kafka event publisher unavailable; using local bus: %s", exc)
        return local


__all__ = [
    "KafkaEventConsumer",
    "KafkaEventProducer",
    "MirroredEventBus",
    "build_runtime_event_bus",
    "kafka_client_config",
]
