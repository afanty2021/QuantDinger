import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask, g

from app.routes import alpaca
from app.services.alpaca_trading import AlpacaClient, AlpacaConfig
from app.utils.broker_session import BrokerSessionRegistry


def test_connection_request_cannot_override_alpaca_endpoint():
    with pytest.raises(ValueError, match="ALPACA_BASE_URL_OVERRIDE_NOT_ALLOWED"):
        alpaca._config_from_request({
            "apiKey": "AKtest",
            "secretKey": "secret",
            "paper": False,
            "baseUrl": "http://169.254.169.254",
        })


@pytest.fixture
def workspace(monkeypatch):
    app = Flask(__name__)
    rows = [
        {"id": number, "name": f"account-{number}", "api_key_hint": "PK...test",
         "encrypted_config": json.dumps({"api_key": f"PK{number}", "secret_key": "secret", "paper": True})}
        for number in (11, 22)
    ]
    monkeypatch.setattr(alpaca, "_saved_alpaca_rows", lambda user_id: rows if user_id == 7 else [])
    monkeypatch.setattr(alpaca, "decrypt_credential_blob", lambda value: value)
    monkeypatch.setattr(alpaca, "_sessions", BrokerSessionRegistry("alpaca"))
    return app


def test_credential_selection_is_explicit_and_owned(workspace):
    with workspace.test_request_context("/?credential_id=22"):
        g.user_id = 7
        assert alpaca._load_saved_alpaca_config(7)["api_key"] == "PK22"
        with pytest.raises(ValueError, match="accountUnavailable"):
            alpaca._load_saved_alpaca_config(8)
    for query, error in [("", "accountSelectionRequired"), ("?credential_id=99", "accountUnavailable"), ("?credential_id=oops", "accountSelectionRequired")]:
        with workspace.test_request_context("/" + query):
            g.user_id = 7
            response, status = inspect.unwrap(alpaca.get_status)()
            assert status == 400
            assert error in response.get_json()["error"]


def test_cached_accounts_and_disconnect_are_isolated(workspace):
    first = MagicMock(connected=True)
    second = MagicMock(connected=True)
    with workspace.test_request_context("/?credential_id=11"):
        g.user_id = 7
        alpaca._sessions.set(first, 11)
        alpaca._sessions.set(second, 22)
        assert alpaca._require_connected_client() == (first, None)
    with workspace.test_request_context("/?credential_id=22"):
        g.user_id = 7
        assert alpaca._require_connected_client() == (second, None)
        inspect.unwrap(alpaca.disconnect)()
        assert alpaca._sessions.get(11) is first
        assert alpaca._sessions.get(22) is None
        second.disconnect.assert_called_once()
        first.disconnect.assert_not_called()
        g.user_id = 8
        assert alpaca._sessions.get(11) is None


def test_account_listing_does_not_expose_credentials(workspace):
    with workspace.test_request_context("/"):
        g.user_id = 7
        data = inspect.unwrap(alpaca.get_saved_accounts)().get_json()["data"]
        assert [item["id"] for item in data] == [11, 22]
        assert all(set(item) == {"id", "name", "api_key_hint"} for item in data)


@pytest.mark.parametrize("failed", [False, True])
def test_overview_counts_distinguish_empty_from_failed(workspace, monkeypatch, failed):
    client = MagicMock()
    client.get_account_summary.return_value = {"success": True, "daytrade_count": "0"}
    client.get_positions.return_value = []
    client.get_orders.return_value = [{"status": "filled"}, {"status": "new"}, {"status": "partially_filled"}]
    if failed:
        client.get_positions.side_effect = RuntimeError("unavailable")
        client.get_orders.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(alpaca, "_require_connected_client", lambda: (client, None))
    with workspace.test_request_context("/"):
        data = inspect.unwrap(alpaca.get_account)().get_json()["data"]
    assert data["position_count"] == (None if failed else 0)
    assert data["recent_filled_order_count"] == (None if failed else 1)
    assert data["recent_order_limit"] == 100


def test_orders_keep_fill_time_price_and_fractional_quantity(monkeypatch):
    import app.services.alpaca_trading.client as module
    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret"))
    client._trading_client = MagicMock()
    client._trading_client.get_orders.return_value = [SimpleNamespace(
        id="order-1", symbol="AAPL", side="buy", qty="3.192", filled_qty="3.192",
        type="limit", limit_price="335.00", filled_avg_price="333.37", status="filled", submitted_at="2026-09-11T14:00:00Z",
        filled_at="2026-09-11T14:00:01Z", created_at="2026-09-11T14:00:00Z",
    )]
    monkeypatch.setattr(client, "_ensure_connected", lambda: None)
    monkeypatch.setattr(module, "_ensure_alpaca", lambda: {
        "GetOrdersRequest": MagicMock(), "QueryOrderStatus": SimpleNamespace(ALL="all", OPEN="open"),
    })
    order = client.get_orders(raise_on_error=True)[0]
    assert order["filled_at"] == "2026-09-11T14:00:01Z"
    assert order["filled_avg_price"] == 333.37
    assert order["filled_qty"] == 3.192
    assert order["order_type"] == "limit"
    assert order["limit_price"] == 335.0


@pytest.mark.parametrize("status,allowed", [("filled", False), ("canceled", False), ("pending_cancel", False), ("unknown", False), ("partially_filled", True), ("new", True)])
def test_cancel_rechecks_broker_order_state(status, allowed, monkeypatch):
    client = AlpacaClient(AlpacaConfig(api_key="PKtest", secret_key="secret"))
    client._trading_client = MagicMock()
    monkeypatch.setattr(client, "_ensure_connected", lambda: None)
    client._trading_client.get_order_by_id.return_value = SimpleNamespace(status=status)
    assert client.cancel_order("order-1") is allowed
    assert client._trading_client.cancel_order_by_id.call_count == int(allowed)
