ALTER TABLE qd_quick_trades
    ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(100) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_quick_trades_client_order
    ON qd_quick_trades(credential_id, exchange_id, market_type, client_order_id)
    WHERE client_order_id <> '';
