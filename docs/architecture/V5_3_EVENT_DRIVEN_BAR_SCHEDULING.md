# V5.3 Event Protocol and Bar-Close Scheduling

## Decision

Ordinary Strategy V2 bar strategies use a versioned event envelope and a shared
bar-close clock. The first transport is process-local. Producers and consumers
depend on the event contract rather than the transport, so a later Kafka adapter
does not require another strategy-runtime rewrite.

The bar event is a wake-up signal, not market data. A woken strategy reloads its
venue-scoped candle frames and still verifies the completed-bar token and frame
timestamp before evaluating signals.

## Event contract

Every envelope contains:

- `event_id`, `event_type`, and `schema_version`;
- UTC `occurred_at`;
- a deterministic `partition_key`;
- `producer` and structured `payload`;
- optional trace, correlation, and causation identifiers.

The initial contracts are `market.bar.closed.v1`, `strategy.command.v1`,
`order.intent.v1`, and `order.fill.v1`.

Bar partitions contain venue, market, market type, instrument ID, and timeframe.
Order partitions contain exchange, credential, and account type. Equal symbols
on Binance, Bybit, OKX, or any other venue therefore never share a bar partition.

## Scheduling behavior

- one process-level clock publishes one event for each distinct subscribed bar
  stream;
- all strategies sharing that exact stream receive the same event;
- repeated events are coalesced while a strategy is already queued or running;
- an event bypasses the normal polling throttle at the candle boundary;
- idle strategies use a periodic safety wake in case an event is missed;
- strategies with positions, pending orders, protections, or stale prices retain
  the existing risk tick;
- grid and martingale runtimes retain their real-time execution paths.

## Equity handling

Intraday US, Hong Kong, and mainland China equity events are filtered through
their exchange calendars, including session opens, closes, holidays, and lunch
breaks. Daily equity strategies retain the existing completed-session policy;
UTC midnight is not treated as an equity-market close.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `BAR_CLOSE_EVENT_SCHEDULER_ENABLED` | `true` | Enables event-driven bar wakes |
| `BAR_CLOSE_EVENT_GRACE_SEC` | `0.5` | Allows a provider to finalize a closed candle |
| `BAR_EVENT_FALLBACK_WAKE_SEC` | `300` | Safety wake for idle event-driven runtimes |
| `BAR_IDLE_SCHEDULER_ENABLED` | `true` | Enables idle parking and the legacy polling fallback |

## Distributed extraction boundary

The Kafka transport and shadow validation path are implemented in
`V5_4_KAFKA_EVENT_BACKBONE.md`. The next cutover assigns evaluator partitions by
stable strategy ID. Order intent and fill events remain routed by execution
account after the order-gateway idempotency boundary is complete.
