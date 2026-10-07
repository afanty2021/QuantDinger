"""Venue contract fixtures based on the official REST and WS field definitions."""

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.execution_streams.fill_snapshot import prepare_event, combine_pending_snapshot
from app.services.execution_streams.normalizers import (
    parse_okx,
    parse_htx,
    parse_gate,
    parse_bitget,
    parse_alpaca,
    parse_binance,
    parse_bybit,
    parse_ibkr_execution,
)
from app.services.live_trading.fill_accounting import contract_multiplier, cumulative_delta
from app.services.live_trading.base import LiveTradingError


@pytest.mark.parametrize(
    "exchange,metadata,multiplier",
    [
        ("okx", {"ctVal": ".1", "ctValCcy": "ETH"}, 0.1),
        ("okx", {"ctVal": ".01", "ctValCcy": "ETH"}, 0.01),
        ("gate", {"quanto_multiplier": ".0001"}, 0.0001),
        ("htx", {"contract_size": ".001"}, 0.001),
    ],
)
def test_contract_and_spot_quantity_units(exchange, metadata, multiplier):
    client = MagicMock()
    client.get_instrument.return_value = metadata
    client.get_contract.return_value = metadata
    client.get_contract_info.return_value = metadata
    raw = dict(exchange_id=exchange, market_type="swap", symbol="ETH/USDT", quantity=3.5, cumulative_quantity=7)
    actual = prepare_event(raw, client, {})
    assert actual["quantity"] == pytest.approx(3.5 * multiplier)
    assert actual["cumulative_quantity"] == pytest.approx(7 * multiplier)
    spot = prepare_event(dict(raw, market_type="spot", symbol="AAPL/USD"), client, {})
    assert spot["quantity"] == 3.5


@pytest.mark.parametrize("value", [None, "", 0, -1, "NaN", "Infinity"])
@pytest.mark.parametrize("exchange,key", [("okx", "ctVal"), ("gate", "quanto_multiplier"), ("htx", "contract_size")])
def test_unavailable_contract_size_never_defaults_to_one(exchange, key, value):
    client = MagicMock()
    for name in ("get_instrument", "get_contract", "get_contract_info"):
        getattr(client, name).return_value = {key: value}
    with pytest.raises(LiveTradingError):
        contract_multiplier(client, exchange, "ETH/USDT")


def test_inverse_contract_is_not_posted_using_linear_formula():
    client = MagicMock()
    client.get_instrument.return_value = {"ctVal": "100", "ctValCcy": "USD", "ctType": "inverse"}
    with pytest.raises(LiveTradingError):
        contract_multiplier(client, "okx", "BTC/USD")


def test_cumulative_value_delta_handles_fractional_shares_and_partial_fills():
    assert cumulative_delta(1, 100, 2, 105) == (1, 110)
    assert cumulative_delta(0.000000001, 100, 0.000000002, 105) == pytest.approx((0.000000001, 110))
    assert cumulative_delta(2, 105, 1, 100) == (0, 0)


def test_htx_real_top_level_trade_array_uses_global_ids_and_contract_symbol():
    payload = {
        "topic": "matchOrders_cross.ETH-USDT",
        "contract_code": "ETH-USDT",
        "symbol": "ETH",
        "order_id_str": "123456789123456789",
        "direction": "buy",
        "status": 6,
        "trade": [
            {"id": "7-123456789123456789-1", "trade_id": 7, "trade_volume": 2, "trade_price": 100},
            {"id": "7-123456789123456789-2", "trade_id": 7, "trade_volume": 3, "trade_price": 110},
        ],
    }
    events = parse_htx(payload, market_type="swap")
    assert [event.quantity for event in events] == [2, 3]
    assert all(event.symbol == "ETH/USDT" for event in events)
    assert events[0].event_key() != events[1].event_key()
    assert all(event.fee_status == "pending" for event in events)


def test_bitget_order_size_is_not_executed_quantity():
    event = parse_bitget(
        {
            "arg": {"channel": "orders", "instType": "SPOT"},
            "data": [
                {
                    "symbol": "BTCUSDT",
                    "size": "1000",
                    "tradeId": "1",
                    "baseVolume": ".001",
                    "accBaseVolume": ".003",
                    "priceAvg": "65000",
                    "fillPrice": "66000",
                    "fillFee": "-.01",
                    "fillFeeCoin": "USDT",
                }
            ],
        }
    )[0]
    assert event.quantity == 0.001
    assert event.price == 66000
    assert event.cumulative_average_price == 65000


@pytest.mark.parametrize("symbol,asset,market", [("AAPL", "us_equity", "usstock"), ("BTC/USD", "crypto", "spot")])
def test_alpaca_asset_class_and_fractional_fill(symbol, asset, market):
    event = parse_alpaca(
        {
            "stream": "trade_updates",
            "data": {
                "event": "partial_fill",
                "qty": ".000000001",
                "price": "110",
                "order": {
                    "symbol": symbol,
                    "asset_class": asset,
                    "filled_qty": ".000000002",
                    "filled_avg_price": "105",
                },
            },
        }
    )[0]
    assert event.market_type == market
    assert event.quantity == 1e-9
    assert event.cumulative_average_price == 105


def test_ibkr_cumulative_average_is_distinct_from_last_price():
    event = parse_ibkr_execution(
        SimpleNamespace(shares=0.2, cumQty=0.5, price=110, avgPrice=105), SimpleNamespace(symbol="AAPL")
    )
    assert (event.quantity, event.cumulative_quantity, event.price, event.cumulative_average_price) == (
        0.2,
        0.5,
        110,
        105,
    )


def test_binance_acknowledgement_is_not_a_fill():
    assert parse_binance({"e": "executionReport", "t": -1, "l": "0", "X": "NEW"}, market_type="spot") == []


def test_bybit_non_trade_and_inverse_events_do_not_enter_linear_ledger():
    assert parse_bybit({"topic": "execution", "data": [{"execQty": 1, "execType": "Funding"}]}) == []
    assert parse_bybit({"topic": "execution", "data": [{"execQty": 1, "category": "inverse"}]}) == []


@pytest.mark.parametrize("symbol", ["00700/HKD", "AAPL/USD", "BTC/USDT"])
def test_gate_spot_symbol_and_quantity_are_not_contracts(symbol):
    event = parse_gate(
        {
            "channel": "spot.usertrades",
            "result": [{"id": "1", "currency_pair": symbol.replace("/", "_"), "amount": ".5", "price": "100"}],
        },
        market_type="spot",
    )[0]
    assert event.quantity == 0.5
    assert event.symbol == symbol
    assert event.fee_status == "pending"


def test_pending_limit_and_market_legs_use_aggregate_quantity_and_value():
    pending = {
        "exchange_response_json": {
            "phases": {
                "executor": {
                    "limit_summary": {
                        "exchange_order_id": "limit",
                        "filled_qty": 1,
                        "avg_price": 100,
                        "fees_by_ccy": {"USDT": 0.1},
                    },
                    "market_summary": {"exchange_order_id": "market", "filled_qty": 1, "avg_price": 110},
                }
            }
        }
    }
    event = {"exchange_order_id": "market", "cumulative_quantity": 2, "cumulative_average_price": 110}
    snapshot, _ = combine_pending_snapshot(event, pending)
    assert snapshot["cumulative_quantity"] == 3
    assert snapshot["cumulative_average_price"] == pytest.approx(320 / 3)
    assert snapshot["_other_fees"] == {"USDT": 0.1}


@pytest.mark.parametrize("value,expected", [("-0.1", 0.1), ("0.1", -0.1), ("0", 0)])
def test_bitget_rest_fee_sign_and_rebates(value, expected):
    from app.services.live_trading.bitget_fees import fee_breakdown

    assert fee_breakdown({"feeCoin": "USDT", "totalFee": value}) == {"USDT": expected}


def test_bitget_new_fee_payload_uses_supplied_settlement_currency():
    from app.services.live_trading.bitget_fees import fee_breakdown

    assert fee_breakdown(
        {"newFees": {"d": "0", "r": "-0.5662405"}},
        received_currency="USDT",
    ) == {"USDT": 0.5662405}


def test_fee_adapter_and_executor_preserve_mixed_rebate():
    from app.services.live_trading.executors import _merge_fee_breakdowns
    from app.services.pending_orders.live_order_support import FillAccumulator

    assert _merge_fee_breakdowns({"USDT": -0.1}, {"BGB": 0.2}) == {"USDT": -0.1, "BGB": 0.2}
    acc = FillAccumulator()
    acc.apply_fee(-0.1, "USDT")
    assert acc.total_fee == -0.1


def test_bitget_spot_order_size_does_not_become_filled_quantity(monkeypatch):
    from app.services.live_trading.bitget_spot import BitgetSpotClient

    client = BitgetSpotClient(api_key="test", secret_key="test", passphrase="test")
    monkeypatch.setattr(client, "get_fills", lambda **kw: {"data": []})
    monkeypatch.setattr(client, "get_order", lambda **kw: {"data": {"size": "1000", "price": "100", "status": "live"}})
    result = client.wait_for_fill(symbol="BTC/USDT", order_id="1", max_wait_sec=0)
    assert result["filled"] == 0
    assert result["avg_price"] == 0


def test_okx_rest_dispatch_and_contract_conversion(monkeypatch):
    from app.services.live_trading.okx import OkxClient
    from app.services.grid.exchange_orders import wait_grid_market_fill

    client = OkxClient(api_key="test", secret_key="test", passphrase="test")
    monkeypatch.setattr(client, "get_instrument", lambda **kw: {"ctVal": ".1", "ctValCcy": "ETH", "ctType": "linear"})
    monkeypatch.setattr(
        client,
        "get_order",
        lambda **kw: {"accFillSz": ".52", "avgPx": "100", "fee": ".001", "feeCcy": "USDT", "state": "filled"},
    )
    details = {}
    quantity, price = wait_grid_market_fill(
        client,
        symbol="ETH/USDT",
        market_type="swap",
        exchange_config={},
        exchange_order_id="1",
        details=details,
        max_wait_sec=0,
    )
    assert (quantity, price) == pytest.approx((0.052, 100))
    assert details["fees_by_ccy"] == {"USDT": -0.001}


def test_gate_stock_rest_dispatch_preserves_fractional_shares(monkeypatch):
    from app.services.live_trading.gate import GateStockClient
    from app.services.grid.exchange_orders import wait_grid_market_fill, query_grid_order_fill

    client = GateStockClient(api_key="test", secret_key="test")
    monkeypatch.setattr(
        client,
        "get_order",
        lambda **kw: {
            "fill_volume": ".125",
            "avg_fill_price": "200",
            "status_desc": "filled",
            "commission": ".02",
            "quote_currency": "USD",
        },
    )
    details = {}
    quantity, average = wait_grid_market_fill(
        client,
        symbol="AAPL/USD",
        market_type="spot",
        exchange_config={},
        exchange_order_id="1",
        details=details,
        max_wait_sec=0,
    )
    assert (quantity, average) == (0.125, 200)
    assert query_grid_order_fill(client, symbol="AAPL/USD", market_type="spot", exchange_order_id="1")[:2] == (
        quantity,
        average,
    )


def test_gate_multiple_fee_currencies_are_not_added_together(monkeypatch):
    from app.services.live_trading.gate import GateSpotClient

    client = GateSpotClient(api_key="test", secret_key="test")
    monkeypatch.setattr(
        client,
        "_signed_request",
        lambda *a, **kw: [{"fee_currency": "BTC", "fee": ".0001"}, {"fee_currency": "GT", "fee": ".2"}],
    )
    native = {}
    amount, currency = client.get_spot_trades_for_order(order_id="1", currency_pair="BTC_USDT", details=native)
    assert (amount, currency) == (0, "MIXED")
    assert native == {"BTC": 0.0001, "GT": 0.2}


def test_bitget_trade_history_paginates_before_aggregation(monkeypatch):
    from app.services.live_trading.bitget import BitgetMixClient

    client = BitgetMixClient(api_key="test", secret_key="test", passphrase="test")
    calls = []

    def request(*args, **kwargs):
        calls.append(dict(kwargs["params"]))
        return (
            {"data": {"fillList": [{"tradeId": str(i)} for i in range(100)], "endId": "99"}}
            if len(calls) == 1
            else {"data": {"fillList": [{"tradeId": "100"}]}}
        )

    monkeypatch.setattr(client, "_signed_request", request)
    result = client.get_order_fills(symbol="BTC/USDT", product_type="USDT-FUTURES", order_id="1")
    assert len(result["data"]["fillList"]) == 101
    assert calls[1]["idLessThan"] == "99"


def test_cumulative_fee_update_has_distinct_durable_key():
    from app.services.execution_streams.events import ExecutionEvent, FeeComponent

    event = ExecutionEvent(
        exchange_id="okx",
        market_type="spot",
        symbol="ETH/USDT",
        exchange_fill_id="1",
        cumulative_quantity=1,
        cumulative_average_price=100,
        fees_cumulative=True,
    )
    first = event.event_key()
    event.fees = [FeeComponent(currency="USDT", amount=0.1)]
    assert event.event_key() != first


def test_snapshot_cache_defers_a_newer_execution(monkeypatch):
    from app.services.execution_streams import fill_snapshot as module

    module._snapshots.clear()
    module._request_times.clear()
    calls = []

    def snapshot(*args, **kwargs):
        calls.append(1)
        kwargs["details"].update(fees_by_ccy={"USDT": 0.1}, status="partial")
        return 1, 100

    monkeypatch.setattr("app.services.grid.exchange_orders.wait_grid_market_fill", snapshot)
    event = dict(
        exchange_id="bybit",
        credential_id=1,
        market_type="swap",
        symbol="BTC/USDT",
        exchange_order_id="1",
        _client=object(),
        _exchange_config={},
        cumulative_quantity=0,
    )
    assert module.complete_snapshot(event)["cumulative_quantity"] == 1
    with pytest.raises(Exception, match="fillSnapshotNotReady"):
        module.complete_snapshot(dict(event, cumulative_quantity=2))
    assert len(calls) == 1
    module._snapshots.clear()
    module._request_times.clear()


@pytest.mark.parametrize("commission,expected", [("0", {"HKD": 0.0}), (None, {})])
def test_gate_stock_missing_average_is_not_replaced_by_limit_price(monkeypatch, commission, expected):
    from app.services.live_trading.gate import GateStockClient

    client = GateStockClient(api_key="test", secret_key="test")
    monkeypatch.setattr(
        client,
        "get_order",
        lambda **kw: {"fill_volume": "1", "price": "420", "status_desc": "filled", "commission": commission},
    )
    result = client.wait_for_fill(order_id="1", symbol="00700/HKD", max_wait_sec=0)
    assert result["avg_price"] == 0
    assert result["fees_by_ccy"] == expected


def test_bitget_detail_retains_multiple_native_fee_currencies(monkeypatch):
    from app.services.live_trading.bitget import BitgetMixClient

    client = BitgetMixClient(api_key="test", secret_key="test", passphrase="test")
    monkeypatch.setattr(client, "get_order_fills", lambda **kw: {"data": {"fillList": []}})
    monkeypatch.setattr(
        client,
        "get_order_detail",
        lambda **kw: {
            "data": {
                "baseVolume": "1",
                "priceAvg": "100",
                "state": "filled",
                "feeDetail": [{"feeCoin": "USDT", "totalFee": "-.1"}, {"feeCoin": "BGB", "totalFee": "-.2"}],
            }
        },
    )
    result = client.wait_for_fill(symbol="ETH/USDT", product_type="USDT-FUTURES", order_id="1", max_wait_sec=0)
    assert result["fees_by_ccy"] == {"USDT": 0.1, "BGB": 0.2}
    assert result["fee_ccy"] == "MIXED"


def test_bitget_mix_fill_maps_new_fee_payload_to_contract_settlement_currency(monkeypatch):
    from app.services.live_trading.bitget import BitgetMixClient

    client = BitgetMixClient(api_key="test", secret_key="test", passphrase="test")
    monkeypatch.setattr(
        client,
        "get_order_fills",
        lambda **kw: {
            "data": {
                "fillList": [
                    {
                        "baseVolume": "0.0113",
                        "price": "83516.30",
                        "feeDetail": {"newFees": {"d": "0", "r": "-0.5662405"}},
                    }
                ]
            }
        },
    )

    result = client.wait_for_fill(
        symbol="BTC/USDT",
        product_type="USDT-FUTURES",
        order_id="1",
        max_wait_sec=0,
    )

    assert result["fees_by_ccy"] == {"USDT": 0.5662405}
    assert result["fee_ccy"] == "USDT"


def test_trade_api_normalizes_legacy_unknown_stable_quote_fee_to_dollars():
    from app.routes.strategy_ledger_routes import _normalize_trade_row_for_api

    row = _normalize_trade_row_for_api(
        {
            "symbol": "BTC/USDT",
            "exchange_id": "bitget",
            "commission": 0.5662405,
            "commission_ccy": "UNKNOWN",
            "commission_quote": None,
        },
        market_type="swap",
    )

    assert row["commission_quote"] == 0.5662405
    assert row["commission_ccy"] == "USDT"


def test_grid_initial_recovery_cannot_invent_trade_from_account_position(monkeypatch):
    from app.services.grid import engine as module

    engine = object.__new__(module.GridEngine)
    engine.strategy_id = 1
    engine._initial_exchange_delta = lambda side: 10
    engine._has_initial_market_trade = lambda: False
    engine._create_client = lambda: object()
    engine._probe_initial_client_order_fill = MagicMock(return_value=False)
    writer = MagicMock()
    monkeypatch.setattr(module, "record_grid_market_fill", writer)
    assert not engine._try_recover_initial_from_exchange(100, "open_long", "grid_initial_long")
    engine._probe_initial_client_order_fill.assert_called_once()
    writer.assert_not_called()


def test_htx_unfilled_order_volume_is_not_executed_quantity():
    from app.services.grid.fill_units import extract_grid_fill_base_qty
    from app.services.live_trading.htx import HtxClient

    client = MagicMock(spec=HtxClient)
    assert (
        extract_grid_fill_base_qty(
            client, symbol="ETH/USDT", market_type="swap", exchange_config={}, data={"trade_volume": 0, "volume": 100}
        )
        == 0
    )
