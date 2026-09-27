from uuid import UUID
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.alpaca_trading.client import (
    AlpacaClient,
    AlpacaConfig,
    _as_str_id,
    _format_alpaca_error,
    _id_log_prefix,
    _normalize_equity_price,
)


def test_as_str_id_from_uuid():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    assert _as_str_id(uid) == "12345678-1234-5678-1234-567812345678"
    assert _id_log_prefix(uid) == "12345678-123"


def test_equity_prices_follow_alpaca_minimum_price_increment():
    assert _normalize_equity_price("220.3169937133789") == 220.32
    assert _normalize_equity_price("0.12345678") == 0.1235


def test_sub_penny_rejection_is_not_reported_as_authentication_failure():
    raw = (
        '{"code":42210000,"message":"invalid take_profit.limit_price '
        '218.53509811401366, sub-penny increment does not fulfill minimum pricing criteria"}'
    )

    message = _format_alpaca_error(RuntimeError(raw))

    assert message == raw
    assert "authentication failed" not in message.lower()


def test_alpaca_authentication_error_is_still_classified():
    raw = '{"code":40110000,"message":"request is not authorized"}'

    message = _format_alpaca_error(RuntimeError(raw))

    assert "authentication failed" in message.lower()


@patch("app.services.alpaca_trading.client._ensure_alpaca")
def test_alpaca_connect_stores_string_account_id(mock_ensure):
    mock_modules = {
        "TradingClient": MagicMock(),
        "StockHistoricalDataClient": MagicMock(),
        "CryptoHistoricalDataClient": MagicMock(),
    }
    mock_ensure.return_value = mock_modules

    account = MagicMock()
    account.id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    account.status = "ACTIVE"

    trading = MagicMock()
    trading.get_account.return_value = account
    mock_modules["TradingClient"].return_value = trading

    client = AlpacaClient(
        AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True)
    )
    assert client.connect() is True
    assert client._account_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert isinstance(client._account_id, str)
    mock_modules["StockHistoricalDataClient"].assert_called_once_with(
        api_key="PKtest", secret_key="secret", sandbox=False
    )
    mock_modules["CryptoHistoricalDataClient"].assert_called_once_with(
        api_key="PKtest", secret_key="secret", sandbox=False
    )


@patch("app.services.alpaca_trading.client.time.sleep", return_value=None)
@patch("app.services.alpaca_trading.client._ensure_alpaca")
def test_crypto_market_sell_caps_quantity_to_available_position(mock_ensure, _mock_sleep):
    market_request = MagicMock()
    modules = {
        "MarketOrderRequest": market_request,
        "OrderSide": SimpleNamespace(BUY="buy", SELL="sell"),
        "TimeInForce": SimpleNamespace(GTC="gtc", DAY="day"),
    }
    mock_ensure.return_value = modules
    trading = MagicMock()
    trading.get_all_positions.return_value = [SimpleNamespace(symbol="BTCUSD", qty="0.000349125")]
    order = SimpleNamespace(
        id="order-1",
        filled_qty="0.000349125",
        filled_avg_price="100000",
        status=SimpleNamespace(value="filled"),
        submitted_at="now",
    )
    trading.submit_order.return_value = order
    trading.get_order_by_id.return_value = order

    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True))
    client._trading_client = trading
    client._account_id = "account-1"

    result = client.place_market_order("BTC/USD", "sell", 0.00035, "crypto")

    assert result.success is True
    market_request.assert_called_once_with(
        symbol="BTC/USD", qty=0.000349125, side="sell", time_in_force="gtc"
    )
    assert result.raw["requested_qty"] == 0.00035
    assert result.raw["submitted_qty"] == 0.000349125


@patch("app.services.alpaca_trading.client.time.sleep", return_value=None)
@patch("app.services.alpaca_trading.client._ensure_alpaca")
def test_equity_market_sell_caps_quantity_to_available_fractional_position(mock_ensure, _mock_sleep):
    market_request = MagicMock()
    modules = {
        "MarketOrderRequest": market_request,
        "OrderSide": SimpleNamespace(BUY="buy", SELL="sell"),
        "TimeInForce": SimpleNamespace(GTC="gtc", DAY="day"),
    }
    mock_ensure.return_value = modules
    trading = MagicMock()
    trading.get_all_positions.return_value = [SimpleNamespace(symbol="SPY", qty="12.654518329")]
    order = SimpleNamespace(
        id="order-2",
        filled_qty="12.654518329",
        filled_avg_price="743.46",
        status=SimpleNamespace(value="filled"),
        submitted_at="now",
    )
    trading.submit_order.return_value = order
    trading.get_order_by_id.return_value = order

    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True))
    client._trading_client = trading
    client._account_id = "account-1"

    result = client.place_market_order("SPY", "sell", 12.65451833, "USStock")

    assert result.success is True
    market_request.assert_called_once_with(
        symbol="SPY", qty=12.654518329, side="sell", time_in_force="day"
    )
    assert result.raw["submitted_qty"] == 12.654518329


@patch("app.services.alpaca_trading.client.time.sleep", return_value=None)
@patch("app.services.alpaca_trading.client._ensure_alpaca")
def test_equity_market_bracket_prices_are_normalized(mock_ensure, _mock_sleep):
    market_request = MagicMock()
    take_profit_request = MagicMock()
    stop_loss_request = MagicMock()
    modules = {
        "MarketOrderRequest": market_request,
        "TakeProfitRequest": take_profit_request,
        "StopLossRequest": stop_loss_request,
        "OrderSide": SimpleNamespace(BUY="buy", SELL="sell"),
        "TimeInForce": SimpleNamespace(GTC="gtc", DAY="day"),
        "OrderClass": SimpleNamespace(BRACKET="bracket", OTO="oto"),
    }
    mock_ensure.return_value = modules
    trading = MagicMock()
    order = SimpleNamespace(
        id="order-3",
        filled_qty="0",
        filled_avg_price=None,
        status=SimpleNamespace(value="accepted"),
        submitted_at="now",
    )
    trading.submit_order.return_value = order
    trading.get_order_by_id.return_value = order

    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True))
    client._trading_client = trading
    client._account_id = "account-1"

    result = client.place_market_order(
        "NVDA",
        "buy",
        2.3,
        "USStock",
        take_profit_price=220.3169937133789,
        stop_loss_price=209.123456789,
    )

    assert result.success is True
    take_profit_request.assert_called_once_with(limit_price=220.32)
    stop_loss_request.assert_called_once_with(stop_price=209.12)


@patch("app.services.alpaca_trading.client.time.sleep", return_value=None)
@patch("app.services.alpaca_trading.client._ensure_alpaca")
def test_market_order_uses_deterministic_client_order_id(mock_ensure, _mock_sleep):
    market_request = MagicMock()
    mock_ensure.return_value = {
        "MarketOrderRequest": market_request,
        "OrderSide": SimpleNamespace(BUY="buy", SELL="sell"),
        "TimeInForce": SimpleNamespace(GTC="gtc", DAY="day"),
    }
    trading = MagicMock()
    order = SimpleNamespace(
        id="order-client-id",
        client_order_id="qd_4_99",
        filled_qty="0",
        filled_avg_price=None,
        status=SimpleNamespace(value="accepted"),
        submitted_at="now",
    )
    trading.submit_order.return_value = order
    trading.get_order_by_id.return_value = order
    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True))
    client._trading_client = trading
    client._account_id = "account-1"

    result = client.place_market_order(
        "AAPL",
        "buy",
        1,
        "USStock",
        client_order_id="qd_4_99",
    )

    market_request.assert_called_once_with(
        symbol="AAPL",
        qty=1,
        side="buy",
        time_in_force="day",
        client_order_id="qd_4_99",
    )
    assert result.raw["client_order_id"] == "qd_4_99"


def test_order_status_can_recover_by_client_order_id():
    trading = MagicMock()
    trading.get_order_by_client_id.return_value = SimpleNamespace(id="recovered-order")
    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True))
    client._trading_client = trading
    client._account_id = "account-1"
    expected = MagicMock()
    client.get_order_status = MagicMock(return_value=expected)

    result = client.get_order_status_by_client_id("qd_4_99")

    assert result is expected
    trading.get_order_by_client_id.assert_called_once_with("qd_4_99")
    client.get_order_status.assert_called_once_with("recovered-order")


@patch("app.services.alpaca_trading.client._ensure_alpaca")
def test_recent_orders_include_filled_orders_by_default(mock_ensure):
    request_factory = MagicMock()
    mock_ensure.return_value = {
        "GetOrdersRequest": request_factory,
        "QueryOrderStatus": SimpleNamespace(ALL="all", OPEN="open"),
    }
    trading = MagicMock()
    trading.get_orders.return_value = [
        SimpleNamespace(
            id="filled-order",
            symbol="AAPL",
            side=SimpleNamespace(value="buy"),
            qty="2",
            notional=None,
            order_type=SimpleNamespace(value="market"),
            limit_price=None,
            status=SimpleNamespace(value="filled"),
            filled_qty="2",
            filled_avg_price="210.5",
            submitted_at="2026-07-17T01:00:00Z",
            extended_hours=False,
        )
    ]
    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret", paper=True))
    client._trading_client = trading
    client._account_id = "account-1"

    orders = client.get_orders()

    request_factory.assert_called_once_with(status="all", limit=100)
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled"] == 2.0
    assert orders[0]["remaining"] == 0.0
