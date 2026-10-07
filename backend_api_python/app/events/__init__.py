"""Versioned event contracts and process-local delivery primitives."""

from app.events.bus import EventBus, EventSubscription, InMemoryEventBus
from app.events.kafka import (
    KafkaEventConsumer,
    KafkaEventProducer,
    MirroredEventBus,
    build_runtime_event_bus,
)
from app.events.protocol import (
    EventEnvelope,
    MarketBarClosedV1,
    OrderFillEventV1,
    OrderIntentEventV1,
    StrategyCommandV1,
    StrategyEvaluationBatchV1,
)

__all__ = [
    "EventEnvelope",
    "EventBus",
    "EventSubscription",
    "InMemoryEventBus",
    "KafkaEventConsumer",
    "KafkaEventProducer",
    "MarketBarClosedV1",
    "OrderFillEventV1",
    "OrderIntentEventV1",
    "MirroredEventBus",
    "StrategyCommandV1",
    "StrategyEvaluationBatchV1",
    "build_runtime_event_bus",
]
