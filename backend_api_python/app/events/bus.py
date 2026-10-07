"""Thread-safe process-local event transport implementing the shared contract."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Protocol

from app.events.protocol import EventEnvelope
from app.utils.logger import get_logger


logger = get_logger(__name__)
EventHandler = Callable[[EventEnvelope], None]


class EventBus(Protocol):
    def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
        *,
        partition_key: str = "*",
    ) -> "EventSubscription": ...

    def publish(self, event: EventEnvelope) -> int: ...

    def snapshot(self) -> dict[str, int]: ...

    def close(self) -> None: ...


class EventSubscription:
    def __init__(self, close_callback: Callable[[], None]) -> None:
        self._close_callback = close_callback
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_callback()

    def __enter__(self) -> "EventSubscription":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class InMemoryEventBus:
    """Fan out envelopes locally while keeping producers transport-neutral."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[tuple[str, str], set[EventHandler]] = defaultdict(set)
        self._published = 0
        self._delivery_errors = 0

    def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
        *,
        partition_key: str = "*",
    ) -> EventSubscription:
        key = (str(event_type), str(partition_key or "*"))
        with self._lock:
            self._subscribers[key].add(handler)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._subscribers.get(key)
                if handlers is None:
                    return
                handlers.discard(handler)
                if not handlers:
                    self._subscribers.pop(key, None)

        return EventSubscription(unsubscribe)

    def publish(self, event: EventEnvelope) -> int:
        with self._lock:
            handlers = set(self._subscribers.get((event.event_type, event.partition_key), ()))
            handlers.update(self._subscribers.get((event.event_type, "*"), ()))
            self._published += 1
        delivered = 0
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception:
                with self._lock:
                    self._delivery_errors += 1
                logger.exception(
                    "Event handler failed: type=%s partition=%s",
                    event.event_type,
                    event.partition_key,
                )
        return delivered

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "event_subscriptions": sum(len(handlers) for handlers in self._subscribers.values()),
                "events_published": self._published,
                "event_delivery_errors": self._delivery_errors,
            }

    def close(self) -> None:
        with self._lock:
            self._subscribers.clear()


__all__ = ["EventBus", "EventSubscription", "InMemoryEventBus"]
