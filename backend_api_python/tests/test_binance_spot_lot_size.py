"""Binance spot LOT_SIZE normalization (MARKET_LOT_SIZE step=0 fallback)."""
from decimal import Decimal

from app.services.live_trading.binance_spot import BinanceSpotClient
from app.services.live_trading.execution import place_order_from_signal


def test_pick_lot_filter_falls_back_when_market_step_zero():
    fdict = {
        "LOT_SIZE": {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
        "MARKET_LOT_SIZE": {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0"},
    }
    picked = BinanceSpotClient._pick_lot_filter(fdict, for_market=True)
    assert picked.get("filterType") == "LOT_SIZE"


def test_normalize_quantity_floors_to_lot_step_for_market_order(monkeypatch):
    client = BinanceSpotClient(api_key="k", secret_key="s")

    def _fake_filters(*, symbol: str):
        return {
            "LOT_SIZE": {
                "stepSize": "0.00001000",
                "minQty": "0.00001000",
            },
            "MARKET_LOT_SIZE": {
                "stepSize": "0",
                "minQty": "0",
            },
            "_meta": {"quantityPrecision": 8, "pricePrecision": 2},
        }

    monkeypatch.setattr(client, "get_symbol_filters", _fake_filters)
    q_dec, prec = client._normalize_quantity(
        symbol="BTC/USDT",
        quantity=0.40106737397126224,
        for_market=True,
    )
    assert q_dec == Decimal("0.40106")
    assert prec == 5


def test_quantity_sized_spot_buy_uses_binance_base_quantity(monkeypatch):
    client = BinanceSpotClient(api_key="k", secret_key="s")

    def _fake_filters(*, symbol: str):
        return {
            "LOT_SIZE": {"stepSize": "0.00001000", "minQty": "0.00001000"},
            "MARKET_LOT_SIZE": {"stepSize": "0", "minQty": "0"},
            "MIN_NOTIONAL": {"minNotional": "1"},
            "_meta": {"quantityPrecision": 8, "pricePrecision": 2},
        }

    monkeypatch.setattr(client, "get_symbol_filters", _fake_filters)
    submitted = {}

    def _submit(_method, _path, *, params=None, **_kwargs):
        submitted.update(params or {})
        return {"orderId": "1", "executedQty": "0", "cummulativeQuoteQty": "0"}

    monkeypatch.setattr(client, "_signed_request", _submit)

    place_order_from_signal(
        client,
        signal_type="open_long",
        symbol="BTC/USDT",
        amount=0.00123456,
        market_type="spot",
        spot_buy_by_quantity=True,
    )

    assert submitted["quantity"] == "0.00123"
    assert "quoteOrderQty" not in submitted
