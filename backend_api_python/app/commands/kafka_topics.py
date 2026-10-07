"""Create the Kafka topics used by the runtime event backbone."""

from __future__ import annotations

import os

from app.events.kafka import kafka_client_config
from app.events.topics import topic_specs
from app.utils.logger import get_logger


logger = get_logger(__name__)


def ensure_topics(admin_client=None) -> list[str]:
    from confluent_kafka import KafkaError
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = admin_client or AdminClient(
        kafka_client_config(client_id=os.getenv("KAFKA_ADMIN_CLIENT_ID", "quantdinger-topic-init"))
    )
    specs = topic_specs()
    futures = admin.create_topics([
        NewTopic(
            spec.name,
            num_partitions=spec.partitions,
            replication_factor=spec.replication_factor,
            config=dict(spec.config),
        )
        for spec in specs
    ])
    created: list[str] = []
    for spec in specs:
        try:
            futures[spec.name].result(timeout=float(os.getenv("KAFKA_ADMIN_TIMEOUT_SEC", "30")))
            created.append(spec.name)
            logger.info(
                "Kafka topic created: topic=%s partitions=%s replication=%s",
                spec.name,
                spec.partitions,
                spec.replication_factor,
            )
        except Exception as exc:
            kafka_error = exc.args[0] if exc.args else None
            if getattr(kafka_error, "code", lambda: None)() == KafkaError.TOPIC_ALREADY_EXISTS:
                logger.info("Kafka topic already exists: topic=%s", spec.name)
                continue
            raise
    return created


def main() -> None:
    ensure_topics()


if __name__ == "__main__":
    main()
