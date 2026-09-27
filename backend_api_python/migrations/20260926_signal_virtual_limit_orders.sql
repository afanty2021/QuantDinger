ALTER TABLE qd_strategy_virtual_orders
    ADD COLUMN IF NOT EXISTS limit_price DECIMAL(24,10) NOT NULL DEFAULT 0;

ALTER TABLE qd_strategy_virtual_orders
    ALTER COLUMN filled_at DROP DEFAULT;

CREATE INDEX IF NOT EXISTS idx_virtual_orders_open_strategy
ON qd_strategy_virtual_orders(strategy_id, strategy_run_id, status)
WHERE status = 'open';
