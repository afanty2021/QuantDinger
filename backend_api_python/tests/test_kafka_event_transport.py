from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.events.bus import InMemoryEventBus
from app.events.kafka import KafkaEventConsumer, KafkaEventProducer, MirroredEventBus
from app.events.protocol import MarketBarClosedV1, StrategyEvaluationBatchV1
from app.events.topics import topic_for_event, topic_specs


def _bar_event():
    return MarketBarClosedV1.create(
        venue="binance",
        market="Crypto",
        market_type="swap",
        instrument_id="BTCUSDT",
        symbol="BTC/USDT",
        timeframe="1m",
        closed_bar_token=123,
        closed_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )


class FakeProducer:
    def __init__(self, *, delivery_error=None):
        self.messages = []
        self.polled = []
        self.flushed = []
        self.delivery_error = delivery_error

    def produce(self, **kwargs):
        self.messages.append(kwargs)

    def poll(self, timeout):
        self.polled.append(timeout)

    def flush(self, timeout):
        self.flushed.append(timeout)
        for message in self.messages:
            message["on_delivery"](self.delivery_error, object())
        return 0


class FakeMessage:
    def __init__(self, value: bytes, error=None, *, offset: int = 0):
        self._value = value
        self._error = error
        self._offset = offset

    def value(self):
        return self._value

    def error(self):
        return self._error

    def topic(self):
        return "qd.market.bar.closed.v1"

    def partition(self):
        return 0

    def offset(self):
        return self._offset


class FakeConsumer:
    def __init__(self, messages):
        self.messages = list(messages)
        self.topics = []
        self.commits = []
        self.seeks = []
        self.closed = False
        self.on_revoke = None
        self.on_lost = None

    def subscribe(self, topics, on_revoke=None, on_lost=None):
        self.topics = list(topics)
        self.on_revoke = on_revoke
        self.on_lost = on_lost

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, **kwargs):
        self.commits.append(kwargs)

    def seek(self, topic_partition):
        self.seeks.append(topic_partition)

    def close(self):
        self.closed = True


def test_kafka_producer_uses_topic_and_exchange_scoped_partition_key():
    fake = FakeProducer()
    producer = KafkaEventProducer(producer=fake)
    event = _bar_event()

    producer.publish(event)

    message = fake.messages[0]
    assert message["topic"] == "qd.market.bar.closed.v1"
    assert message["key"] == b"bar:binance:crypto:swap:btcusdt:1m"
    assert event.event_id.encode() in message["value"]
    message["on_delivery"](None, object())
    assert producer.snapshot() == {
        "kafka_events_queued": 1,
        "kafka_events_delivered": 1,
        "kafka_delivery_errors": 0,
    }


def test_kafka_batch_reports_delivery_callback_failure():
    fake = FakeProducer(delivery_error=RuntimeError("broker rejected message"))
    producer = KafkaEventProducer(producer=fake)

    assert producer.publish_batch([_bar_event()], timeout=3) is False
    assert producer.snapshot()["kafka_delivery_errors"] == 1


def test_mirrored_bus_keeps_local_delivery_when_kafka_publish_fails():
    class BrokenProducer:
        def publish(self, _event):
            raise RuntimeError("broker unavailable")

        def snapshot(self):
            return {}

        def close(self):
            return None

    local = InMemoryEventBus()
    bus = MirroredEventBus(local_bus=local, kafka_producer=BrokenProducer())
    received = []
    bus.subscribe(MarketBarClosedV1.event_type, received.append)

    assert bus.publish(_bar_event()) == 1
    assert len(received) == 1
    assert bus.snapshot()["kafka_event_publish_errors"] == 1


def test_consumer_commits_only_after_handler_success():
    event = _bar_event()
    fake = FakeConsumer([
        FakeMessage(event.to_json().encode()),
        FakeMessage(event.to_json().encode()),
    ])
    consumer = KafkaEventConsumer(
        topics=["qd.market.bar.closed.v1"],
        group_id="test-group",
        consumer=fake,
    )

    assert consumer.poll_once(lambda _event: False)
    assert fake.commits == []
    assert fake.seeks[0].offset == 0
    assert consumer.poll_once(lambda _event: True)
    assert len(fake.commits) == 1
    assert consumer.snapshot()["kafka_messages_handled"] == 1


def test_consumer_releases_partition_keys_before_rebalance():
    event = StrategyEvaluationBatchV1.create(
        strategy_shard=17,
        strategy_ids=[17],
        source_event_id="bar-event-id",
        closed_bar_token=123,
        timeframe="1m",
    )
    fake = FakeConsumer([FakeMessage(event.to_json().encode())])
    revoked = []
    consumer = KafkaEventConsumer(
        topics=["qd.strategy.evaluate.v1"],
        group_id="test-group",
        consumer=fake,
        on_partitions_revoked=revoked.append,
    )

    assert consumer.poll_once(lambda _event: True)
    fake.on_revoke(
        fake,
        [SimpleNamespace(topic="qd.market.bar.closed.v1", partition=0)],
    )

    assert revoked == [{"strategy-shard:17"}]
    assert consumer.snapshot()["kafka_tracked_partitions"] == 0


def test_strategy_evaluation_batch_has_deterministic_shard_identity():
    first = StrategyEvaluationBatchV1.create(
        strategy_shard=17,
        strategy_ids=[9, 3, 9],
        source_event_id="bar-event-id",
        closed_bar_token=123,
        timeframe="1m",
        batch_index=2,
    )
    second = StrategyEvaluationBatchV1.create(
        strategy_shard=17,
        strategy_ids=[3, 9],
        source_event_id="bar-event-id",
        closed_bar_token=123,
        timeframe="1m",
        batch_index=2,
    )

    assert first.event_id == second.event_id
    assert first.partition_key == "strategy-shard:17"
    assert first.payload["strategy_ids"] == [3, 9]
    assert topic_for_event(first.event_type) == "qd.strategy.evaluate.v1"


def test_topic_defaults_fit_single_node_and_can_scale_up(monkeypatch):
    for name in (
        "KAFKA_MARKET_PARTITIONS",
        "KAFKA_STRATEGY_PARTITIONS",
        "KAFKA_ORDER_PARTITIONS",
        "KAFKA_DLQ_PARTITIONS",
    ):
        monkeypatch.delenv(name, raising=False)

    specs = {spec.name: spec for spec in topic_specs()}
    assert specs["qd.market.bar.closed.v1"].partitions == 6
    assert specs["qd.strategy.evaluate.v1"].partitions == 12
    assert specs["qd.order.intent.v1"].partitions == 12
    assert specs["qd.runtime.dlq.v1"].partitions == 3
