from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.services import community_service
from app.routes import ibkr as ibkr_route
from app.services.community_service import (
    CommunityService,
    _credit_marketplace_seller,
    _debit_marketplace_buyer,
    _lock_marketplace_accounts,
)
from app.services.ibkr_trading import client as ibkr_client_module
from app.services.ibkr_trading.client import IBKRClient
from app.services.live_trading import factory


class _Cursor:
    def __init__(self, rows=None, row=None):
        self.rows = list(rows or [])
        self.row = row
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


def test_marketplace_accounts_are_locked_in_stable_order():
    cur = _Cursor(rows=[{"id": 2, "credits": "5.00"}, {"id": 9, "credits": "100.00"}])

    balances = _lock_marketplace_accounts(cur, buyer_id=9, seller_id=2)

    query, params = cur.calls[0]
    assert params == (2, 9)
    assert "ORDER BY id FOR UPDATE" in query
    assert balances == {2: Decimal("5.00"), 9: Decimal("100.00")}


def test_marketplace_debit_is_relative_guarded_and_returns_actual_balance():
    cur = _Cursor(row={"credits": "70.00"})

    balance = _debit_marketplace_buyer(cur, buyer_id=9, amount=Decimal("30.00"))

    query, params = cur.calls[0]
    assert "credits = credits - ?" in query
    assert "credits >= ?" in query
    assert "RETURNING credits" in query
    assert params == (Decimal("30.00"), 9, Decimal("30.00"))
    assert balance == Decimal("70.00")


def test_marketplace_debit_reports_racing_insufficient_balance():
    cur = _Cursor(row=None)

    assert _debit_marketplace_buyer(cur, buyer_id=9, amount=Decimal("30.00")) is None


def test_marketplace_seller_credit_is_relative_and_returns_actual_balance():
    cur = _Cursor(row={"credits": "35.00"})

    balance = _credit_marketplace_seller(cur, seller_id=2, amount=Decimal("30.00"))

    query, params = cur.calls[0]
    assert "credits = credits + ?" in query
    assert "RETURNING credits" in query
    assert params == (Decimal("30.00"), 2)
    assert balance == Decimal("35.00")


def test_marketplace_purchase_uses_shared_transaction(monkeypatch):
    used = {"transaction": 0}

    class Transaction:
        def __enter__(self):
            used["transaction"] += 1
            raise RuntimeError("transaction sentinel")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(community_service, "get_db_transaction", lambda: Transaction())
    service = CommunityService.__new__(CommunityService)

    success, message, data = service.purchase_indicator(9, 4)

    assert success is False
    assert message == "error: transaction sentinel"
    assert data == {}
    assert used["transaction"] == 1


def test_ibkr_client_policy_blocks_before_loading_sdk(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_DESKTOP_BROKERS", "false")
    ensure_sdk = Mock()
    monkeypatch.setattr(ibkr_client_module, "_ensure_ib_insync", ensure_sdk)

    assert IBKRClient().connect() is False
    ensure_sdk.assert_not_called()


def test_ibkr_existing_client_operations_obey_disabled_policy(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_DESKTOP_BROKERS", "false")
    client = IBKRClient()

    with pytest.raises(PermissionError):
        client._ensure_connected()


def test_ibkr_factory_policy_blocks_before_client_creation(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_DESKTOP_BROKERS", "false")
    client_class = Mock()
    monkeypatch.setattr(factory, "IBKRClient", client_class)

    with pytest.raises(PermissionError):
        factory.create_ibkr_client({"ibkr_host": "127.0.0.1"})

    client_class.assert_not_called()


def test_ibkr_connect_route_returns_forbidden_before_client_creation(app, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_DESKTOP_BROKERS", "false")
    client_class = Mock()
    monkeypatch.setattr(ibkr_route, "IBKRClient", client_class)
    view = ibkr_route.connect
    while hasattr(view, "__wrapped__"):
        view = view.__wrapped__

    with app.test_request_context("/api/ibkr/connect", method="POST", json={"host": "10.0.0.8"}):
        result = view()

    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, result.status_code

    assert status == 403
    assert response.get_json()["success"] is False
    client_class.assert_not_called()
