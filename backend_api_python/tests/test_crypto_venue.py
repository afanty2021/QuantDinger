from app.data_sources.crypto import resolve_crypto_venue, resolve_native_public_exchange


def test_resolve_crypto_venue_swap_from_trading_config():
    ex, mt = resolve_crypto_venue(
        exchange_config={"exchange_id": "binance"},
        trading_config={"market_type": "swap"},
    )
    assert ex == "binance"
    assert mt == "swap"


def test_resolve_crypto_venue_defaults_to_settings_exchange():
    ex, mt = resolve_crypto_venue(
        exchange_config={},
        trading_config={"market_type": "spot"},
    )
    assert ex
    assert mt == "spot"


def test_binance_swap_maps_to_native_swap_client():
    exchange_id, market_type = resolve_native_public_exchange("binance", "swap")
    assert (exchange_id, market_type) == ("binance", "swap")


def test_binance_spot_maps_to_native_spot_client():
    exchange_id, market_type = resolve_native_public_exchange("binance", "spot")
    assert (exchange_id, market_type) == ("binance", "spot")
