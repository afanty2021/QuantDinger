# V5.1 Shared Market Data Upgrade

## Scope

V5.1 reduces duplicate public exchange connections and idle runtime work without
changing order execution, position ownership, strategy code, or private account
streams.

The release contains two independently reversible changes:

1. process-local public price-feed sharing;
2. adaptive waits for flat crypto bar strategies.

## Public feed identity

A feed is reusable only when all of the following match:

- exchange ID;
- normalized market type;
- API family;
- exchange instrument ID;
- canonical strategy symbol key;
- complete subscription set.

Therefore Binance BTC/USDT, Bybit BTC/USDT, Binance spot BTC/USDT, and Binance
swap BTC/USDT are four independent price domains. Private order and fill streams
are never shared by this registry.

The first implementation shares exact subscription sets. Subscription packing
across overlapping sets is intentionally deferred until the exact-set path has
passed production soak tests.

## Runtime behavior

Strategies with an active position, pending order, pending protection exit,
stale price, grid runtime, or martingale runtime retain their configured risk
tick. A flat crypto strategy with no outstanding work may wait up to the next
closed-bar boundary, capped by `BAR_IDLE_WAKE_INTERVAL_SEC`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `SHARED_PUBLIC_MARKET_FEED_ENABLED` | `true` | Enable process-local public feed reuse |
| `SHARED_PUBLIC_MARKET_FALLBACK_TTL_SEC` | `1` | Coalesce fallback REST calls |
| `BAR_IDLE_SCHEDULER_ENABLED` | `true` | Enable adaptive waits for safe idle runtimes |
| `BAR_IDLE_WAKE_INTERVAL_SEC` | `60` | Maximum idle wait before a health/runtime check |

Disabling either feature restores its previous execution path without a schema
migration.

## Initial acceptance targets

- one feed for identical exchange/market/instrument subscription sets;
- no public price reuse across exchanges or spot/swap markets;
- at least 90 percent fewer fallback calls during a shared one-second window;
- at least 80 percent fewer idle cycles for eligible one-minute strategies;
- no change to active-position risk tick latency;
- no duplicate order intents during disconnect, reconnect, or worker shutdown.

The follow-up runtime upgrade replaces the one-thread-per-strategy supervisor
with a bounded cooperative evaluator pool. Shared feeds and adaptive waits are
the input and scheduling primitives used by that pool.
