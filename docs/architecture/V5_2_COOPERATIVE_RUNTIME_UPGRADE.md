# V5.2 Cooperative Runtime Upgrade

## Decision

Strategy V2 runtimes execute on a fixed-size cooperative evaluator pool. The
one-thread-per-strategy runtime is removed rather than retained behind a legacy
engine selector.

Each active strategy owns a generator and durable runtime identity. A generator
runs one risk or signal cycle, yields its next due delay, and is then rescheduled
on the bounded evaluator pool. One strategy is never evaluated concurrently.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `STRATEGY_MAX_ACTIVE` | `100000` | Admission ceiling for active runtimes in one worker |
| `STRATEGY_EVALUATOR_THREADS` | `16` | Fixed evaluator thread count |
| `BAR_IDLE_WAKE_INTERVAL_SEC` | `60` | Maximum wait for an eligible idle bar runtime |

`STRATEGY_MAX_ACTIVE` is not a capacity guarantee. Proven admission limits must
remain lower than the configured ceiling until load tests pass.

## Lifecycle

- start commands create a runtime generator and schedule it on the evaluator;
- restore schedules desired runtimes without a serial three-second ready wait;
- the first successful yield marks a runtime ready;
- stop commands set the runtime stop event and wake a sleeping generator;
- runtime cleanup flushes state, releases shared feeds, and closes the run;
- leases and fencing remain authoritative for execution permission.

The runtime loop no longer queries `qd_strategies_trading.status` every cycle.
Desired-state changes must flow through the durable strategy command path.

## Price isolation

Public feed reuse remains scoped by exchange, normalized market type, API
family, exchange instrument ID, canonical symbol key, and complete subscription
set. Equal symbols on different exchanges never share a price cache.

## Remaining hyperscale work

The evaluator pool removes the OS-thread multiplier but does not by itself prove
100,000-strategy capacity. The next constraints are:

1. per-strategy position and order reads;
2. duplicated in-memory session and Pandas frame state;
3. process-local rather than cluster-wide market-data sharing;
4. direct order execution without account-partitioned gateways;
5. lack of a durable partitioned event backbone;
6. realtime protection scans for active positions.

The next release should extract immutable compiled artifacts, shared candle
windows, and partition-owned hot state before distributing evaluator partitions
across multiple processes.
