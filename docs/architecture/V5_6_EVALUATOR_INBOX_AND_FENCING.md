# V5.6 Evaluator Inbox and Fencing

> Historical phase record: the evaluator was shadow-only in V5.6. The current
> evaluator hosts hot Strategy V2 runtimes and emits fenced order intents for
> eligible closed-bar strategies; see [V5.7](V5_7_DISTRIBUTED_BAR_RUNTIME.md).

## Outcome

This phase adds the reliability boundary required before strategy calculation can move from the local trading worker to a horizontally scaled Kafka consumer group.

The new `strategy-evaluator-worker` consumes `qd.strategy.evaluate.v1` in shadow mode. It does not execute strategy code or submit orders yet. It validates every batch, verifies current running strategies, claims a stable strategy shard, and records completion in a durable inbox.

## Delivery model

Kafka provides at-least-once delivery. PostgreSQL provides the durable decision about whether an evaluator event has already completed.

```text
strategy.evaluate.v1
        |
        +--> claim qd_event_inbox
        |       |
        |       +--> completed/dead: commit duplicate
        |       +--> busy: retry Kafka offset
        |       +--> acquired: continue
        |
        +--> acquire qd_strategy_shard_leases
        |
        +--> validate active strategies
        |
        +--> complete inbox --> commit Kafka offset
```

An evaluator never commits the Kafka offset before the inbox row is completed. Retrying the same deterministic event ID therefore does not evaluate it twice after cutover.

## Fencing

`qd_strategy_shard_leases` stores an owner and a monotonically increasing fencing token for each logical shard. A new owner can take over only after the prior lease expires. Ownership changes increment the token; renewals by the same owner preserve it.

Kafka consumer-group ownership prevents normal concurrent consumption. The database fence covers the dangerous transition window where an old worker may still be finishing work after a rebalance or network partition.

The next execution phase must carry this fencing token into runtime ownership and order-intent validation. The order gateway must reject work from an older token before distributed evaluation becomes authoritative.

## Scaling

The evaluator has no fixed container name and can be scaled on one host or across hosts:

```powershell
docker compose up -d --scale strategy-evaluator-worker=8
```

All replicas share one consumer group and PostgreSQL inbox. Single-node mode is simply one replica of the same service.

## Database placement

No service assumes PostgreSQL is on the same machine. All access uses `DATABASE_URL`. Moving PostgreSQL to a dedicated server later requires changing connection and network/TLS settings, not strategy code, Kafka schemas, or table ownership.

For a production database host, connection pooling should move behind PgBouncer and PostgreSQL should have dedicated storage, backups, replication, and monitoring. This deployment change is intentionally deferred; the current phase only preserves that boundary.

## Cutover gate

The evaluator remains in `shadow` mode until these conditions are met:

1. Dispatched and completed batch counts match over a sustained workload.
2. Duplicate delivery, consumer restart, rebalance, and expired-lease tests pass.
3. Runtime state can be restored on another evaluator without restarting strategy history.
4. Fencing tokens are enforced by the order-intent and execution layers.
5. A shard-percentage switch can move a bounded subset of strategies to distributed evaluation and roll it back safely.
