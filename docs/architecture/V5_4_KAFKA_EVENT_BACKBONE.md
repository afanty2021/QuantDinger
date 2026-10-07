# V5.4 Kafka Event Backbone

> Historical phase record: Kafka was a shadow path in V5.4. The current
> deployment has completed the V5.7 cutover for eligible closed-bar strategies;
> see [V5.7](V5_7_DISTRIBUTED_BAR_RUNTIME.md) and the
> [current deployment guide](../deployment/DISTRIBUTED_RUNTIME_SCALING.md).

## Outcome

This phase introduces Kafka as the durable inter-process event backbone without changing the current exchange order execution path. The trading worker still wakes local strategy runtimes through the in-process event bus. Every versioned event is also published to Kafka and validated by an independent audit consumer.

The deployment is intentionally asymmetric:

1. Local event delivery is the live path.
2. Kafka publishing is a fail-open shadow path.
3. Kafka consumer failures cannot stop strategy evaluation or order execution.
4. Kafka becomes authoritative only after event parity, replay, and fault-injection acceptance tests pass.

## Runtime flow

```text
exchange-scoped bar clock
        |
        +--> in-process event bus --> cooperative strategy scheduler (live path)
        |
        +--> Kafka producer --> qd.market.bar.closed.v1 --> audit consumer (shadow path)
```

The partition key for market bars is:

```text
bar:{venue}:{market}:{market_type}:{instrument_id}:{timeframe}
```

This is a correctness boundary, not only a scaling key. Prices for the same symbol on Binance, Gate.io, OKX, a stock venue, or another provider remain separate. Crypto spot and perpetual instruments also remain separate. Events for one key retain partition order.

## Topics

| Topic | Default partitions | Retention | Intended role |
| --- | ---: | ---: | --- |
| `qd.market.bar.closed.v1` | 6 | 3 days | Closed market-bar boundaries |
| `qd.strategy.lifecycle.v1` | 12 | 7 days | Start, stop, pause, and configuration commands |
| `qd.strategy.evaluate.v1` | 12 | 3 days | Bounded strategy evaluation batches |
| `qd.order.intent.v1` | 12 | 30 days | Durable order intents after the execution cutover |
| `qd.order.event.v1` | 12 | 30 days | Accepted, rejected, filled, and reconciliation events |
| `qd.runtime.dlq.v1` | 3 | 30 days | Quarantined messages after bounded retries |

The defaults target a single 8-core/16-GiB host. Partition counts can be increased online before adding more consumer replicas, but should not be reduced or changed casually after production traffic starts because repartitioning changes key placement and replay behavior. The 100k-strategy cluster profile should be benchmarked with 96 or 128 strategy-event partitions, at least three brokers, and `KAFKA_REPLICATION_FACTOR=3`; the one-click stack uses one combined KRaft broker and replication factor one.

## Delivery guarantees

The producer uses `acks=all`, Kafka idempotence, keyed messages, Zstandard compression, and asynchronous delivery. The consumer uses manual commits and `read_committed` isolation. An offset is committed only after the handler succeeds.

These guarantees do not make exchange orders exactly once. Kafka cannot atomically commit an exchange API side effect. Before order execution moves behind Kafka, the gateway must enforce all of the following:

- a stable client order ID derived from the intent ID;
- a database inbox unique constraint for consumed event IDs;
- an outbox transaction for emitted order events;
- per-account fencing so only one executor owns an account partition;
- exchange reconciliation before retrying an uncertain submission.

## Deployment controls

- `KAFKA_EVENT_PUBLISH_ENABLED=true` publishes runtime events to the Kafka execution backbone.
- Docker Compose enables event publishing for the trading worker by default.
- `kafka-init` creates the versioned topics before the audit worker starts.
- `kafka-audit-worker` validates market events and records counters plus the last observed event in `qd_worker_heartbeats`.
- Kafka producer delivery counters are included in the trading worker heartbeat through the runtime capacity snapshot.

The API and scheduler processes do not depend on Kafka. The trading worker also does not wait for Kafka at startup; an unavailable broker increments shadow-publish errors while local live execution continues.

## Next cutover

The next phase replaces local per-process market-event fan-out with two explicit stages:

1. The due-strategy dispatcher now consumes `qd.market.bar.closed.v1`, resolves durable subscriptions, groups them by stable strategy shard, and publishes bounded `strategy.evaluate.v1` batches. See [V5.5 Strategy Shard Dispatch](./V5_5_STRATEGY_SHARD_DISPATCH.md).
2. Evaluator workers will consume strategy shards in a consumer group, acquire or verify runtime ownership, evaluate each strategy once for the closed-bar token, and persist an inbox record before committing the Kafka offset.

After shadow parity is demonstrated, traffic can move by shard percentage rather than by a global flag. The order gateway remains unchanged during this scheduler cutover.
