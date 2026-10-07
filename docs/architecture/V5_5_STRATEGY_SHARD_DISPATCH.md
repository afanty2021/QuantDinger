# V5.5 Strategy Shard Dispatch

> Historical phase record: dispatch was audited in parallel during V5.5. It is
> now the active path for eligible closed-bar strategies when distributed bar
> mode is enabled; see [V5.7](V5_7_DISTRIBUTED_BAR_RUNTIME.md).

## Outcome

This phase fixes the deployment boundary needed for both a single-node installation and a later multi-node cluster. A running strategy registers the exact market-bar streams it consumes. A dispatcher consumer group resolves each closed-bar event into bounded strategy-evaluation batches keyed by a stable logical strategy shard.

The existing in-process scheduler remains the authoritative execution path during this phase. Kafka dispatch runs in parallel so its output can be audited before evaluator ownership moves out of the trading worker.

## Flow

```text
venue-specific bar close
        |
        +--> local event bus --> local evaluator pool (authoritative)
        |
        +--> qd.market.bar.closed.v1
                    |
                    +--> dispatcher consumer group
                              |
                              +--> qd.strategy.evaluate.v1
                                      keyed by strategy-shard:{0..127}
```

`qd_strategy_event_subscriptions` is the routing index. It maps a versioned event type and a full market partition key to the running strategies that consume it. The lookup does not scan every strategy definition on each bar close.

## Market identity

The routing key includes all of the following dimensions:

```text
bar:{venue}:{market}:{market_type}:{instrument_id}:{timeframe}
```

This keeps prices and bar boundaries separate for the same display symbol across Binance, Gate.io, OKX, Alpaca, and other providers. It also separates crypto spot, perpetual contracts, and stock products. Stock strategies use the configured exchange or data provider as `venue`; their session-aware bar clock still suppresses events outside completed exchange sessions.

## Stable strategy shards

The default logical shard count is 128 and a strategy maps to `strategy_id % STRATEGY_SHARD_COUNT`. Logical shards are stable when worker instances are added or removed. Kafka assigns shard keys to physical topic partitions and its consumer group distributes those partitions across available workers.

`STRATEGY_EVALUATION_BATCH_SIZE` defaults to 250. Large fan-outs are split into deterministic batches. Reprocessing the same source event produces the same batch event IDs, which is required for the evaluator inbox deduplication in the next phase.

Do not change `STRATEGY_SHARD_COUNT` after production cutover without a controlled resharding operation.

## Single-node and scale-out deployment

A single machine runs one instance of each service and uses the same event schemas, topics, subscription table, and shard keys as a cluster. There is no separate single-node strategy implementation.

The dispatcher has no fixed container name and can be increased without configuration changes:

```powershell
docker compose up -d --scale strategy-dispatcher-worker=4
```

All replicas use `KAFKA_STRATEGY_DISPATCH_GROUP_ID=quantdinger-strategy-dispatch-v1`, so one Kafka partition is owned by only one dispatcher replica at a time. Adding replicas redistributes partitions; it does not duplicate the subscription database or require moving strategy rows.

Dispatcher scaling alone does not distribute strategy calculation yet. A scalable shadow evaluator consumer group, durable event inbox, and shard fencing are now implemented in [V5.6 Evaluator Inbox and Fencing](./V5_6_EVALUATOR_INBOX_AND_FENCING.md). Hot-state recovery and order-layer fencing remain required before cutover. Until that cutover is accepted, the local cooperative scheduler continues evaluating and submitting orders.

## Operational controls

- `STRATEGY_SHARD_COUNT=128` controls logical strategy shards.
- `STRATEGY_EVALUATION_BATCH_SIZE=250` caps one evaluation message.
- `KAFKA_STRATEGY_DISPATCH_GROUP_ID` identifies the dispatcher consumer group.
- `KAFKA_DISPATCH_FLUSH_TIMEOUT_SEC=10` bounds acknowledgement wait before the source offset is retried.
- The `strategy-dispatcher` heartbeat records consumed events, dispatched strategies, batches, and Kafka delivery counters.

The dispatcher commits a source offset only after all generated batches have been acknowledged by Kafka. A failed flush causes the source event to be retried.
