"""Low-overhead PostgreSQL pool metrics."""

from prometheus_client import Counter, Gauge, Histogram


DB_POOL_ACQUIRE_SECONDS = Histogram(
    "quantdinger_db_pool_acquire_seconds",
    "Time spent waiting to acquire a PostgreSQL connection.",
    ("application",),
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DB_POOL_ACQUIRE_FAILURES = Counter(
    "quantdinger_db_pool_acquire_failures_total",
    "PostgreSQL connection acquisition failures.",
    ("application", "reason"),
)
DB_POOL_CONNECTIONS = Gauge(
    "quantdinger_db_pool_connections",
    "PostgreSQL pool connections by state.",
    ("application", "state"),
    multiprocess_mode="livesum",
)
DB_POOL_UTILIZATION_RATIO = Gauge(
    "quantdinger_db_pool_utilization_ratio",
    "Highest per-process PostgreSQL pool utilization ratio.",
    ("application",),
    multiprocess_mode="max",
)


__all__ = [
    "DB_POOL_ACQUIRE_FAILURES",
    "DB_POOL_ACQUIRE_SECONDS",
    "DB_POOL_CONNECTIONS",
    "DB_POOL_UTILIZATION_RATIO",
]
