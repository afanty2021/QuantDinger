CREATE TABLE IF NOT EXISTS qd_event_radar_analyses (
    id BIGSERIAL PRIMARY KEY,
    analysis_uid VARCHAR(64) NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    symbol VARCHAR(80) NOT NULL,
    market_type VARCHAR(24) NOT NULL DEFAULT '',
    direction VARCHAR(16) NOT NULL DEFAULT 'neutral',
    confidence DECIMAL(8, 6),
    impact VARCHAR(16) NOT NULL DEFAULT 'low',
    relevance VARCHAR(16) NOT NULL DEFAULT 'low',
    freshness VARCHAR(16) NOT NULL DEFAULT 'stale',
    summary TEXT NOT NULL DEFAULT '',
    provider VARCHAR(24) NOT NULL DEFAULT 'none',
    model VARCHAR(120) NOT NULL DEFAULT '',
    fallback_reason TEXT NOT NULL DEFAULT '',
    source_status_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    events_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    billing_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_radar_user_symbol
    ON qd_event_radar_analyses(user_id, symbol, market_type, created_at DESC);
