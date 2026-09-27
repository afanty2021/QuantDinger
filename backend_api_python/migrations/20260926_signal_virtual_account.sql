CREATE TABLE IF NOT EXISTS qd_strategy_virtual_accounts (
    strategy_id INTEGER PRIMARY KEY REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    initial_cash DECIMAL(24,8) NOT NULL DEFAULT 0,
    cash_balance DECIMAL(24,8) NOT NULL DEFAULT 0,
    realized_pnl DECIMAL(24,8) NOT NULL DEFAULT 0,
    total_commission DECIMAL(24,8) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qd_strategy_virtual_orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    pending_order_id INTEGER NOT NULL UNIQUE,
    order_intent_id INTEGER NOT NULL DEFAULT 0,
    symbol VARCHAR(80) NOT NULL,
    side VARCHAR(16) NOT NULL DEFAULT '',
    action VARCHAR(32) NOT NULL,
    order_type VARCHAR(16) NOT NULL DEFAULT 'market',
    requested_qty DECIMAL(24,10) NOT NULL DEFAULT 0,
    fill_qty DECIMAL(24,10) NOT NULL DEFAULT 0,
    reference_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    fill_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    leverage DECIMAL(12,4) NOT NULL DEFAULT 1,
    commission_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    commission_quote DECIMAL(24,8) NOT NULL DEFAULT 0,
    slippage_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    slippage_quote DECIMAL(24,8) NOT NULL DEFAULT 0,
    status VARCHAR(24) NOT NULL DEFAULT 'filled',
    reason VARCHAR(255) NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    filled_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qd_strategy_virtual_positions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    symbol VARCHAR(80) NOT NULL,
    symbol_canonical VARCHAR(80) NOT NULL DEFAULT '',
    side VARCHAR(10) NOT NULL,
    size DECIMAL(24,10) NOT NULL DEFAULT 0,
    entry_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    current_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    highest_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    lowest_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    unrealized_pnl DECIMAL(24,8) NOT NULL DEFAULT 0,
    pnl_percent DECIMAL(14,6) NOT NULL DEFAULT 0,
    market_type VARCHAR(20) NOT NULL DEFAULT 'swap',
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(strategy_id, symbol_canonical, side)
);

CREATE TABLE IF NOT EXISTS qd_strategy_virtual_trades (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    virtual_order_id INTEGER NOT NULL UNIQUE REFERENCES qd_strategy_virtual_orders(id) ON DELETE CASCADE,
    pending_order_id INTEGER NOT NULL UNIQUE,
    order_intent_id INTEGER NOT NULL DEFAULT 0,
    symbol VARCHAR(80) NOT NULL,
    symbol_canonical VARCHAR(80) NOT NULL DEFAULT '',
    type VARCHAR(32) NOT NULL,
    side VARCHAR(10) NOT NULL,
    price DECIMAL(24,10) NOT NULL DEFAULT 0,
    amount DECIMAL(24,10) NOT NULL DEFAULT 0,
    value DECIMAL(24,8) NOT NULL DEFAULT 0,
    commission DECIMAL(24,8) NOT NULL DEFAULT 0,
    commission_quote DECIMAL(24,8) NOT NULL DEFAULT 0,
    profit DECIMAL(24,8) NOT NULL DEFAULT 0,
    close_reason VARCHAR(255) NOT NULL DEFAULT '',
    matched_entry_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    account_equity DECIMAL(24,8) NOT NULL DEFAULT 0,
    market_type VARCHAR(20) NOT NULL DEFAULT 'swap',
    exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    leverage DECIMAL(12,4) NOT NULL DEFAULT 1,
    reference_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    commission_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    slippage_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    slippage_quote DECIMAL(24,8) NOT NULL DEFAULT 0,
    fill_source VARCHAR(32) NOT NULL DEFAULT 'virtual_signal',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_virtual_positions_strategy
ON qd_strategy_virtual_positions(strategy_id, symbol_canonical, side);
CREATE INDEX IF NOT EXISTS idx_virtual_trades_strategy_time
ON qd_strategy_virtual_trades(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_virtual_orders_strategy_time
ON qd_strategy_virtual_orders(strategy_id, created_at);

ALTER TABLE qd_strategy_virtual_orders
    ADD COLUMN IF NOT EXISTS exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    ADD COLUMN IF NOT EXISTS leverage DECIMAL(12,4) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS commission_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS commission_quote DECIMAL(24,8) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS slippage_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS slippage_quote DECIMAL(24,8) NOT NULL DEFAULT 0;

ALTER TABLE qd_strategy_virtual_trades
    ADD COLUMN IF NOT EXISTS exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS leverage DECIMAL(12,4) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS reference_price DECIMAL(24,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS commission_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS slippage_rate DECIMAL(14,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS slippage_quote DECIMAL(24,8) NOT NULL DEFAULT 0;
