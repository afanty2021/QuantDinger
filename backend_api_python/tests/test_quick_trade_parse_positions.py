"""Tests for Quick Trade position side parsing (long vs short display)."""

from unittest.mock import MagicMock, patch

import pytest

from app.routes.quick_trade import _infer_position_side_from_row, _parse_positions


def test_binance_hedge_short():
    row = {"positionSide": "SHORT", "positionAmt": "109", "symbol": "APTUSDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_binance_one_way_short_signed_amt():
    row = {"positionSide": "BOTH", "positionAmt": "-109", "symbol": "APTUSDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_bybit_sell_side():
    row = {"side": "Sell", "size": "109", "symbol": "APTUSDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_bybit_position_idx_short():
    row = {"positionIdx": 2, "size": "109", "symbol": "APTUSDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_gate_negative_contract_size():
    row = {"size": -50, "positionAmt": 109.0, "symbol": "APT_USDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_gate_position_side_short():
    row = {"size": -50, "positionAmt": 109.0, "positionSide": "SHORT", "symbol": "APT_USDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_htx_direction_sell():
    row = {"volume": 10, "direction": "sell", "contract_code": "APT-USDT"}
    assert _infer_position_side_from_row(row) == "short"


def test_parse_positions_gate_short_row():
    raw = [{"size": -50, "positionAmt": 109.0, "positionSide": "SHORT", "contract": "APT_USDT"}]
    out = _parse_positions(raw)
    assert len(out) == 1
    assert out[0]["side"] == "short"
    assert out[0]["size"] == 109.0


def test_okx_net_mode_short_negative_pos():
    """OKX 买卖模式: posSide=net + pos<0 必须识别为空仓."""
    row = {
        "instId": "APT-USDT-SWAP",
        "posSide": "net",
        "pos": "-109",
        "avgPx": "0.9129",
        "upl": "0.1526",
    }
    assert _infer_position_side_from_row(row) == "short"


def test_okx_net_mode_long_positive_pos():
    row = {"posSide": "net", "pos": "50", "instId": "APT-USDT-SWAP"}
    assert _infer_position_side_from_row(row) == "long"


def test_okx_long_short_mode_short():
    row = {"posSide": "short", "pos": "109", "instId": "APT-USDT-SWAP"}
    assert _infer_position_side_from_row(row) == "short"


def test_parse_positions_okx_net_mode_wrapper():
    raw = {
        "code": "0",
        "data": [
            {
                "instId": "APT-USDT-SWAP",
                "posSide": "net",
                "pos": "-109",
                "avgPx": "0.9129",
                "upl": "0.1526",
                "markPx": "0.9115",
                "notionalUsd": "99.75",
            }
        ],
    }
    out = _parse_positions(raw)
    assert len(out) == 1
    assert out[0]["side"] == "short"
    assert out[0]["size"] == 109.0
    assert out[0]["notional_usdt"] == 99.75


def test_normalize_okx_positions_raw_net_short():
    from app.routes.quick_trade import _normalize_okx_positions_raw

    raw = {"data": [{"posSide": "net", "pos": "-109"}]}
    norm = _normalize_okx_positions_raw(raw)
    assert norm["data"][0]["positionSide"] == "SHORT"


def test_parse_positions_bybit_list_wrapper():
    raw = {
        "result": {
            "list": [
                {
                    "symbol": "APTUSDT",
                    "side": "Sell",
                    "size": "109",
                    "entryPrice": "0.9129",
                    "unrealisedPnl": "0.15",
                }
            ]
        }
    }
    out = _parse_positions(raw)
    assert len(out) == 1
    assert out[0]["side"] == "short"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "84458.20",
                    "markPrice": "84374.00",
                    "unRealizedProfit": "-0.842",
                    "initialMargin": "168.9164",
                    "leverage": "5",
                }
            ],
            (84458.20, 84374.00, -0.842, 168.9164, 5.0),
        ),
        (
            [
                {
                    "contract": "BTC_USDT",
                    "size": "1",
                    "entry_price": "84458.20",
                    "mark_price": "84374.00",
                    "unrealised_pnl": "-0.842",
                    "initial_margin": "168.9164",
                    "lever": "5",
                }
            ],
            (84458.20, 84374.00, -0.842, 168.9164, 5.0),
        ),
        (
            {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "1",
                        "avgPx": "84458.20",
                        "markPx": "84374.00",
                        "upl": "-0.842",
                        "imr": "168.9164",
                        "lever": "5",
                    }
                ],
            },
            (84458.20, 84374.00, -0.842, 168.9164, 5.0),
        ),
        (
            {
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "size": "0.01",
                            "entryPrice": "84458.20",
                            "markPrice": "84374.00",
                            "unrealisedPnl": "-0.842",
                            "positionIM": "168.9164",
                            "leverage": "5",
                        }
                    ]
                }
            },
            (84458.20, 84374.00, -0.842, 168.9164, 5.0),
        ),
        (
            {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "holdSide": "long",
                        "total": "0.01",
                        "openPriceAvg": "84458.20",
                        "markPrice": "84374.00",
                        "unrealizedPL": "-0.842",
                        "marginSize": "168.9164",
                        "leverage": "5",
                    }
                ]
            },
            (84458.20, 84374.00, -0.842, 168.9164, 5.0),
        ),
        (
            {
                "data": [
                    {
                        "contract_code": "BTC-USDT",
                        "direction": "buy",
                        "volume": "1",
                        "open_avg_price": "84458.20",
                        "mark_price": "84374.00",
                        "profit_unreal": "-0.842",
                        "initial_margin": "168.9164",
                        "lever_rate": "5",
                    }
                ]
            },
            (84458.20, 84374.00, -0.842, 168.9164, 5.0),
        ),
    ],
    ids=["binance", "gate", "okx", "bybit", "bitget", "htx"],
)
def test_parse_positions_exchange_pnl_fields(raw, expected):
    out = _parse_positions(raw)
    assert len(out) == 1
    row = out[0]
    assert (
        row["entry_price"],
        row["mark_price"],
        row["unrealized_pnl"],
        row["initial_margin"],
        row["leverage"],
    ) == pytest.approx(expected)


def test_parse_positions_spot_bal_row():
    """Spot wallet rows use bal/availBal instead of pos/positionAmt."""
    raw = {
        "data": [
            {
                "symbol": "APT/USDT",
                "bal": 120.5,
                "availBal": 118.0,
            }
        ]
    }
    out = _parse_positions(raw)
    assert len(out) == 1
    assert out[0]["side"] == "long"
    assert out[0]["size"] == 120.5


def test_fetch_spot_holdings_raw_empty():
    from app.routes.quick_trade import _fetch_spot_holdings_raw

    client = MagicMock()
    with patch(
        "app.services.live_trading.spot_sizing.get_spot_base_holding",
        return_value={"total": 0.0, "available": 0.0},
    ):
        raw = _fetch_spot_holdings_raw(client, symbol="APT/USDT")
    assert raw == {"data": []}


def test_fetch_spot_holdings_raw_with_balance():
    from app.routes.quick_trade import _fetch_spot_holdings_raw

    client = MagicMock()
    with patch(
        "app.services.live_trading.spot_sizing.get_spot_base_holding",
        return_value={"total": 50.0, "available": 48.5, "avg_cost": 1.25},
    ):
        raw = _fetch_spot_holdings_raw(client, symbol="APT/USDT")
    out = _parse_positions(raw)
    assert len(out) == 1
    assert out[0]["symbol"] == "APT/USDT"
    assert out[0]["size"] == 50.0
    assert out[0]["entry_price"] == 1.25


def test_enrich_spot_positions_computes_pnl():
    from app.routes.quick_trade import _enrich_spot_positions

    client = MagicMock()
    with patch(
        "app.routes.quick_trade._quick_trade_spot_avg_entry_price",
        return_value=2.0,
    ), patch(
        "app.services.live_trading.spot_sizing.fetch_spot_last_price",
        return_value=2.5,
    ):
        out = _enrich_spot_positions(
            [{"symbol": "APT/USDT", "side": "long", "size": 10.0, "entry_price": 0, "mark_price": 0}],
            client=client,
            symbol="APT/USDT",
            user_id=1,
            credential_id=1,
            market_type="spot",
        )
    assert len(out) == 1
    assert out[0]["entry_price"] == 2.0
    assert out[0]["mark_price"] == 2.5
    assert abs(out[0]["unrealized_pnl"] - 5.0) < 1e-9
