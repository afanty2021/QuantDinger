import pytest

from app.data_sources import crypto, native_crypto
from app.services import symbol_master_sync
from app.services.symbol_master_sync import (
    SymbolMasterRow,
    _okx_public_payload_to_rows,
    fetch_crypto_symbols,
    fetch_crypto_symbols_with_diagnostics,
)
from app.services.market.symbol_search import _classify_asset


class FakeExchange:
    def __init__(self, exchange_id, market_type="spot", **_kwargs):
        self.id = exchange_id
        is_swap = market_type in {"swap", "linear"}
        self.markets = {
            "AAPL/USDT:USDT" if is_swap else "AAPL/USDT": {
                "active": True,
                "spot": not is_swap,
                "swap": is_swap,
                "base": "AAPL",
                "quote": "USDT",
                "settle": "USDT" if is_swap else None,
                "id": "AAPLUSDT",
                "displayName": "Apple",
            }
        }

    def load_markets(self, reload=False):
        return self.markets


@pytest.fixture(autouse=True)
def _disable_live_bitget_catalog(monkeypatch, request):
    monkeypatch.setattr(symbol_master_sync, "_load_known_equity_symbols", lambda: {"AAPL", "NVDA"})
    if request.node.name != "test_bitget_reality_catalog_uses_official_native_contract":
        monkeypatch.setattr(symbol_master_sync, "_fetch_bitget_reality_symbol_rows", lambda: [])
    if request.node.name != "test_gate_stock_catalog_uses_dedicated_official_api":
        monkeypatch.setattr(symbol_master_sync, "_fetch_gate_stock_symbol_rows", lambda: [])


def test_bitget_reality_catalog_uses_official_native_contract(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{
                "symbol": "rAAPLUSDT",
                "baseCoin": "rAAPL",
                "quoteCoin": "USDT",
                "isReality": True,
                "status": "online",
                "quantityPrecision": "4",
            }]}

    monkeypatch.setattr(symbol_master_sync.requests, "get", lambda *args, **kwargs: Response())

    rows = symbol_master_sync._fetch_bitget_reality_symbol_rows()

    assert len(rows) == 1
    assert rows[0].symbol == "RAAPL/USDT"
    assert rows[0].instrument_id == "rAAPLUSDT"
    assert rows[0].product_type == "tokenized_equity"
    assert rows[0].api_family == "reality"


def test_gate_stock_catalog_uses_dedicated_official_api(monkeypatch):
    exchanges = []

    class Response:
        def __init__(self, stock_exchange):
            self.stock_exchange = stock_exchange

        def raise_for_status(self):
            return None

        def json(self):
            is_hk = self.stock_exchange == "hk"
            return {"data": {"total_page": 1, "list": [{
                "symbol": "00700" if is_hk else "AAPL",
                "symbol_desc": "Tencent" if is_hk else "Apple Inc.",
                "exchange": self.stock_exchange,
                "quote_currency": "HKD" if is_hk else "USD",
                "settlement_currency": "HKD" if is_hk else "USD",
                "fx_rate": "0.1275" if is_hk else "1",
                "trade_status": "closed",
                "trade_mode": 4,
                "step_order_volume": "0.0001",
                "commission_rate": "0.001",
            }]}}

    def fake_get(*args, **kwargs):
        stock_exchange = kwargs["params"]["exchange"]
        exchanges.append(stock_exchange)
        return Response(stock_exchange)

    monkeypatch.setattr(symbol_master_sync.requests, "get", fake_get)

    rows = symbol_master_sync._fetch_gate_stock_symbol_rows()

    assert exchanges == ["us", "hk"]
    assert len(rows) == 2
    assert rows[0].symbol == "AAPL/USD"
    assert rows[0].instrument_id == "AAPL"
    assert rows[0].product_type == "direct_equity"
    assert rows[0].api_family == "stock"
    assert rows[0].product_meta["status"] == "tradable"
    assert rows[0].product_meta["session_status"] == "closed"
    hk = next(row for row in rows if row.symbol == "00700/HKD")
    assert hk.currency == "HKD"
    assert hk.settle_currency == "HKD"
    assert hk.underlying_market == "HKStock"
    assert hk.underlying_symbol == "00700"
    assert hk.product_meta["stock_exchange"] == "hk"
    assert hk.product_meta["fx_rate"] == "0.1275"


def test_full_catalog_keeps_same_equity_separate_by_venue_and_product(monkeypatch):
    monkeypatch.setattr(native_crypto, "create_native_crypto_client", FakeExchange)

    rows = fetch_crypto_symbols()

    assert len(rows) == 12
    assert {
        (row.exchange, row.market_type)
        for row in rows
    } == {
        (exchange_id, market_type)
        for exchange_id in crypto.PUBLIC_KLINE_EXCHANGE_IDS
        for market_type in ("spot", "swap")
    }


def test_equity_metadata_is_classified_without_ticker_hardcoding():
    assert _classify_asset({"info": {"instCategory": "3"}}) == "equity"
    assert _classify_asset({"info": {"symbolType": "xstocks"}}) == "equity"
    assert _classify_asset({"info": {"symbolType": "stock"}}) == "equity"
    assert _classify_asset({"info": {"isRwa": "YES"}}) == "rwa"
    assert _classify_asset({"info": {}}) == "crypto"


def test_catalog_diagnostics_report_every_venue_product(monkeypatch):
    monkeypatch.setattr(native_crypto, "create_native_crypto_client", FakeExchange)

    rows, contexts = fetch_crypto_symbols_with_diagnostics()

    assert len(rows) == 12
    assert len(contexts) == 12
    assert all(context["ok"] and context["rows"] == 1 for context in contexts)


def test_okx_native_client_uses_configured_public_hostname():
    client = native_crypto.create_native_crypto_client("okx", okx_host="openapi.okx.com")
    assert client._okx_host == "openapi.okx.com"


def test_okx_public_payload_parser_keeps_only_live_usdt_instruments():
    spot_payload = {
        "code": "0",
        "data": [
            {"instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT", "state": "live"},
            {"instId": "ETH-USDC", "baseCcy": "ETH", "quoteCcy": "USDC", "state": "live"},
            {"instId": "OLD-USDT", "baseCcy": "OLD", "quoteCcy": "USDT", "state": "suspend"},
        ],
    }
    swap_payload = {
        "code": "0",
        "data": [
            {"instId": "BTC-USDT-SWAP", "ctValCcy": "BTC", "settleCcy": "USDT", "state": "live"},
            {"instId": "BTC-USD-SWAP", "ctValCcy": "BTC", "settleCcy": "BTC", "state": "live"},
        ],
    }

    spot_rows = _okx_public_payload_to_rows(spot_payload, "spot", _classify_asset)
    swap_rows = _okx_public_payload_to_rows(swap_payload, "swap", _classify_asset)

    assert [(row.symbol, row.instrument_id) for row in spot_rows] == [("BTC/USDT", "BTC-USDT")]
    assert [(row.symbol, row.instrument_id) for row in swap_rows] == [("BTC/USDT", "BTC-USDT-SWAP")]


def test_okx_catalog_uses_official_public_fallback_when_native_client_fails(monkeypatch):
    class FailingExchange:
        def __init__(self, exchange_id, market_type="spot", **_kwargs):
            self.id = exchange_id
            self.markets = {}

        def load_markets(self, reload=False):
            raise RuntimeError("primary endpoint unavailable")

    def client_factory(exchange_id, market_type="spot", **kwargs):
        client_type = FailingExchange if exchange_id == "okx" else FakeExchange
        return client_type(exchange_id, market_type, **kwargs)

    monkeypatch.setattr(native_crypto, "create_native_crypto_client", client_factory)

    def fake_okx_rows(market_type, classify_asset):
        instrument_id = "BTC-USDT" if market_type == "spot" else "BTC-USDT-SWAP"
        return [SymbolMasterRow(
            "Crypto", "BTC/USDT", "BTC", "okx", "USDT", market_type,
            instrument_id, "USDT", "crypto",
        )]

    monkeypatch.setattr(symbol_master_sync, "_fetch_okx_public_symbol_rows", fake_okx_rows)

    rows, contexts = fetch_crypto_symbols_with_diagnostics()
    okx_contexts = [context for context in contexts if context["exchange"] == "okx"]

    assert len(rows) == 12
    assert len(okx_contexts) == 2
    assert all(context["ok"] and context["fallback"] for context in okx_contexts)


def test_stored_equity_products_upgrade_without_remote_catalog(monkeypatch):
    rows = [
        {
            "exchange": "binance",
            "market_type": "spot",
            "symbol": "NVDAB/USDT",
            "instrument_id": "NVDABUSDT",
            "asset_class": "crypto",
        },
        {
            "exchange": "bybit",
            "market_type": "swap",
            "symbol": "AAPL/USDT",
            "instrument_id": "AAPLUSDT",
            "asset_class": "equity",
        },
    ]
    updates = []

    class Cursor:
        rowcount = 1

        def execute(self, query, values=None):
            if query.lstrip().startswith("UPDATE"):
                updates.append(values)

        def fetchall(self):
            return rows

        def close(self):
            return None

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return self.cursor_value

        def commit(self):
            return None

        def rollback(self):
            return None

    monkeypatch.setattr(symbol_master_sync, "get_db_connection", Connection)

    upgraded = symbol_master_sync.reclassify_stored_equity_products()

    assert upgraded == 2
    assert updates[0][:5] == ("equity", "tokenized_equity", "spot", "USStock", "NVDA")
    assert updates[1][:5] == ("equity", "stock_perpetual", "swap", "USStock", "AAPL")
