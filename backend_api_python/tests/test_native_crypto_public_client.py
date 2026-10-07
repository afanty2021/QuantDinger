import pytest

from app.data_sources.native_crypto import NativeCryptoPublicClient


@pytest.mark.parametrize(
    ("exchange_id", "market_type", "expected"),
    [
        ("binance", "spot", "BTCUSDT"),
        ("bybit", "swap", "BTCUSDT"),
        ("bitget", "spot", "BTCUSDT"),
        ("okx", "swap", "BTC-USDT-SWAP"),
        ("gate", "spot", "BTC_USDT"),
        ("htx", "spot", "btcusdt"),
        ("htx", "swap", "BTC-USDT"),
    ],
)
def test_native_symbol_mapping(exchange_id, market_type, expected):
    client = NativeCryptoPublicClient(exchange_id, market_type)
    assert client._native_symbol("BTC/USDT:USDT") == expected


@pytest.mark.parametrize(
    ("exchange_id", "market_type", "payload", "expected_close"),
    [
        (
            "binance",
            "spot",
            [[1700000000000, "100", "110", "90", "105", "12"]],
            105.0,
        ),
        (
            "okx",
            "swap",
            {"code": "0", "data": [["1700000000000", "100", "110", "90", "106", "13"]]},
            106.0,
        ),
        (
            "bybit",
            "spot",
            {
                "retCode": 0,
                "result": {"list": [["1700000000000", "100", "110", "90", "107", "14"]]},
            },
            107.0,
        ),
        (
            "bitget",
            "swap",
            {"code": "00000", "data": [["1700000000000", "100", "110", "90", "108", "15"]]},
            108.0,
        ),
        (
            "gate",
            "spot",
            [["1700000000", "1000", "109", "110", "90", "100", "16"]],
            109.0,
        ),
        (
            "htx",
            "spot",
            {
                "status": "ok",
                "data": [{"id": 1700000000, "open": 100, "high": 110, "low": 90, "close": 111, "amount": 17}],
            },
            111.0,
        ),
    ],
)
def test_ohlcv_parsing(exchange_id, market_type, payload, expected_close, monkeypatch):
    client = NativeCryptoPublicClient(exchange_id, market_type)
    monkeypatch.setattr(client, "_get", lambda *_args, **_kwargs: payload)

    rows = client.fetch_ohlcv("BTC/USDT", "1m", limit=1)

    assert rows[0][:5] == [1700000000000, 100.0, 110.0, 90.0, expected_close]
    assert rows[0][5] > 0


def test_okx_historical_window_uses_forward_compatible_after_cursor(monkeypatch):
    client = NativeCryptoPublicClient("okx", "spot")
    captured = {}

    def fake_get(url, params):
        captured.update(url=url, params=params)
        return {"code": "0", "data": [["1700000000000", "1", "2", "0.5", "1.5", "3"]]}

    monkeypatch.setattr(client, "_get", fake_get)
    client.fetch_ohlcv("BTC/USDT", "1m", since=1699999880000, limit=2)

    assert captured["url"].endswith("/api/v5/market/history-candles")
    assert captured["params"]["after"] == 1700000000000


def test_market_catalog_exposes_perpetual_alias(monkeypatch):
    client = NativeCryptoPublicClient("binance", "swap")
    monkeypatch.setattr(
        client,
        "_get",
        lambda *_args, **_kwargs: {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                }
            ]
        },
    )

    markets = client.load_markets()

    assert markets["BTC/USDT:USDT"]["contract"] is True
    assert markets["BTC/USDT"]["linear"] is True


def test_bybit_spot_catalog_does_not_send_unsupported_pagination(monkeypatch):
    client = NativeCryptoPublicClient("bybit", "spot")
    captured = []

    def fake_get(_url, params):
        captured.append(params)
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "status": "Trading",
                    }
                ]
            },
        }

    monkeypatch.setattr(client, "_get", fake_get)
    markets = client.load_markets()

    assert "BTC/USDT" in markets
    assert captured == [{"category": "spot"}]


@pytest.mark.parametrize("market_type", ["spot", "swap"])
def test_bitget_ticker_parses_change_rate_without_unbound_state(market_type, monkeypatch):
    client = NativeCryptoPublicClient("bitget", market_type)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *_args, **_kwargs: {
            "code": "00000",
            "requestTime": 1700000000000,
            "data": [
                {
                    "symbol": "BTCUSDT",
                    "lastPr": "101.25",
                    "open": "100",
                    "high24h": "103",
                    "low24h": "99",
                    "change24h": "0.0125",
                    "quoteVolume": "2500000",
                    "ts": "1700000000123",
                }
            ],
        },
    )

    ticker = client.fetch_ticker("BTC/USDT")

    assert ticker["last"] == 101.25
    assert ticker["percentage"] == 1.25
    assert ticker["changePercent"] == 1.25
    assert ticker["timestamp"] == 1700000000123
