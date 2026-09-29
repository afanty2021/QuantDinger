import pytest

from app.services.quick_trade import products


def _catalog_product(**overrides):
    product = {
        "market": "Crypto",
        "symbol": "AAPL/USDT",
        "exchange_id": "bitget",
        "market_type": "spot",
        "instrument_id": "rAAPLUSDT",
        "settle_currency": "USDT",
        "product_type": "tokenized_equity",
        "api_family": "reality",
        "product_meta": {"status": "online"},
    }
    product.update(overrides)
    return product


def test_quick_trade_product_identity_selects_specialized_client_config(monkeypatch):
    product = _catalog_product()
    monkeypatch.setattr(products, "build_exchange_config", lambda *args, **kwargs: {"exchange_id": "bitget", "market_type": "spot"})
    monkeypatch.setattr(products, "get_catalog_product", lambda **kwargs: product)
    monkeypatch.setattr(products, "validate_runtime_products", lambda values, **kwargs: values)
    monkeypatch.setattr(products, "validate_product_account_environment", lambda *args, **kwargs: None)

    config, resolved = products.build_product_aware_config(
        7,
        11,
        symbol="AAPL/USDT",
        market_type="spot",
        source={"instrument_id": "rAAPLUSDT", "product_type": "tokenized_equity", "api_family": "reality"},
    )

    assert resolved == product
    assert config["api_family"] == "reality"
    assert config["instrument_id"] == "rAAPLUSDT"
    assert config["instrument_product_meta"] == {"status": "online"}


def test_quick_trade_rejects_product_contract_mismatch(monkeypatch):
    monkeypatch.setattr(products, "build_exchange_config", lambda *args, **kwargs: {"exchange_id": "bitget"})
    monkeypatch.setattr(products, "get_catalog_product", lambda **kwargs: _catalog_product(api_family="spot"))

    with pytest.raises(ValueError, match="equityProductContractChanged"):
        products.build_product_aware_config(
            7,
            11,
            symbol="AAPL/USDT",
            market_type="spot",
            source={"instrument_id": "rAAPLUSDT", "product_type": "tokenized_equity", "api_family": "reality"},
        )


def test_regular_crypto_quick_trade_does_not_require_catalog(monkeypatch):
    monkeypatch.setattr(products, "build_exchange_config", lambda *args, **kwargs: {"exchange_id": "binance", "market_type": "spot"})
    monkeypatch.setattr(products, "get_catalog_product", lambda **kwargs: pytest.fail("catalog lookup should not run"))

    config, resolved = products.build_product_aware_config(
        3,
        5,
        symbol="BTC/USDT",
        market_type="spot",
        source={},
    )

    assert config["exchange_id"] == "binance"
    assert resolved is None


def test_resolve_product_can_fall_back_for_uncatalogued_regular_crypto(monkeypatch):
    monkeypatch.setattr(products, "build_exchange_config", lambda *args, **kwargs: {"exchange_id": "binance", "market_type": "spot"})
    monkeypatch.setattr(products, "get_catalog_product", lambda **kwargs: None)

    config, resolved = products.build_product_aware_config(
        3,
        5,
        symbol="BTC/USDT",
        market_type="spot",
        source={"resolve_product": True},
    )

    assert config["exchange_id"] == "binance"
    assert resolved is None
