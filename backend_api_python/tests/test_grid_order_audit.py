from unittest.mock import MagicMock

import pytest

from app.services.grid import order_audit
from app.services.grid.resting_orders_repo import GridRestingOrder


@pytest.fixture
def audit_setup(monkeypatch):
    strategy = {"id": 3, "market_type": "spot", "exchange_config": {"credential_id": 9}, "trading_config": {}}
    config = {"credential_id": 9, "exchange_id": "bybit", "testnet": True}
    resolve = MagicMock(return_value=config)
    monkeypatch.setattr(order_audit, "resolve_exchange_config", resolve)
    monkeypatch.setattr(order_audit, "bind_instrument_product_contract", lambda cfg, *a, **kw: cfg)
    client = MagicMock()
    monkeypatch.setattr(order_audit, "create_client", lambda cfg, **kw: client)
    # An API worker has no in-memory runner and must not start one for a read.
    monkeypatch.setattr("app.services.grid.runner.get_runner", MagicMock(side_effect=AssertionError("runner lookup")))
    return strategy, resolve, client


def test_api_worker_audit_matches_only_owned_order_ids_and_symbols(monkeypatch, audit_setup):
    strategy, resolve, _ = audit_setup
    rows = [GridRestingOrder(id=n, strategy_id=3, symbol="ETH/USDT", exchange_order_id=str(n)) for n in (1, 2, 3)]
    remote = [
        {"exchange_order_id": "1", "symbol": "ETH/USDT", "status": "New", "price": 2600.01, "amount": .004, "filled": 0},
        {"exchange_order_id": "2", "symbol": "BTC/USDT", "status": "New"},
        {"exchange_order_id": "99", "symbol": "ETH/USDT", "status": "New"},
    ]
    monkeypatch.setattr(order_audit, "fetch_exchange_open_orders", lambda *a, **kw: remote)
    result = order_audit.audit_grid_orders(strategy, rows, user_id=7)
    resolve.assert_called_once_with({"credential_id": 9}, user_id=7)
    assert result["completed"]
    assert result["active"] == 1
    assert result["unknown"] == 2
    assert result["orders"][1]["price"] == 2600.01
    assert result["orders"][2]["status"] == "not_open"
    assert result["orders"][3]["status"] == "not_open"
    assert result["checked_at"].endswith("+00:00")
    assert all(row.status == "pending" for row in rows)


def test_failed_exchange_read_is_not_zero_confirmed_success(monkeypatch, audit_setup):
    strategy, _, _ = audit_setup
    monkeypatch.setattr(order_audit, "fetch_exchange_open_orders", MagicMock(side_effect=TimeoutError()))
    result = order_audit.audit_grid_orders(strategy, [GridRestingOrder(id=1, symbol="ETH/USDT", exchange_order_id="1")], user_id=7)
    assert not result["completed"]
    assert result["error"] == "grid_exchange_snapshot_failed"
    assert result["orders"] == {}
    assert result["checked_at"] is None


@pytest.mark.parametrize("status", ["NEW", "live", "open", "PartiallyFilled", "partially_filled"])
def test_native_open_statuses_are_confirmed(monkeypatch, audit_setup, status):
    strategy, _, _ = audit_setup
    monkeypatch.setattr(order_audit, "fetch_exchange_open_orders", lambda *a, **kw: [{"exchange_order_id": "1", "symbol": "ETH/USDT", "status": status}])
    result = order_audit.audit_grid_orders(strategy, [GridRestingOrder(id=1, symbol="ETH/USDT", exchange_order_id="1")], user_id=7)
    assert result["active"] == 1
    assert not result["error"]


@pytest.mark.parametrize("sync", [True, False])
def test_grid_route_does_not_count_local_ids_as_exchange_confirmation(monkeypatch, sync):
    import inspect
    from flask import Flask, g
    from app.routes import strategy_grid_routes as route

    service = MagicMock()
    service.get_strategy.return_value = {"id": 3, "trading_config": {}, "exchange_config": {"credential_id": 9}}
    monkeypatch.setattr(route, "get_strategy_service", lambda: service)
    monkeypatch.setattr("app.services.strategy_runtime.bot_type.resolve_bot_type", lambda *a, **kw: "grid")
    repo = MagicMock()
    repo.list_for_strategy.return_value = [GridRestingOrder(id=1, strategy_id=3, symbol="ETH/USDT", exchange_order_id="123", status="open")]
    monkeypatch.setattr("app.services.grid.resting_orders_repo.GridRestingOrderRepository", lambda: repo)
    audit = MagicMock(return_value={"active": 1, "completed": True, "checked_at": "2026-09-19T05:00:00+00:00", "orders": {1: {"status": "open", "price": 2600.01}}})
    monkeypatch.setattr(order_audit, "audit_grid_orders", audit)
    app = Flask(__name__)
    with app.test_request_context(f"/?id=3&sync={int(sync)}"):
        g.user_id = 7
        result = inspect.unwrap(route.get_grid_resting_orders)().get_json()["data"]
    service.get_strategy.assert_called_once_with(3, user_id=7)
    assert result["summary"]["verified_exchange_orders"] == int(sync)
    assert result["summary"]["unverified_orders"] == int(not sync)
    assert result["orders"][0]["exchange_status"] == ("open" if sync else "unverified")
    assert audit.call_count == int(sync)


def test_grid_route_returns_virtual_resting_orders_without_exchange_audit(monkeypatch):
    import inspect
    from flask import Flask, g
    from app.routes import strategy_grid_routes as route

    service = MagicMock()
    service.get_strategy.return_value = {
        "id": 3,
        "execution_mode": "signal",
        "trading_config": {},
        "exchange_config": {},
    }
    monkeypatch.setattr(route, "get_strategy_service", lambda: service)
    monkeypatch.setattr("app.services.strategy_runtime.bot_type.resolve_bot_type", lambda *a, **kw: "grid")
    monkeypatch.setattr(
        "app.services.virtual_trading.list_virtual_limit_orders",
        lambda *a, **kw: [{
            "id": 8,
            "strategy_id": 3,
            "symbol": "BTC/USDT",
            "side": "long",
            "action": "open_long",
            "reason": "long_entry",
            "status": "open",
            "limit_price": 95,
            "requested_qty": 2,
            "fill_qty": 0,
            "fill_price": 0,
            "client_order_id": "grid-4-long-entry-1",
        }],
    )
    audit = MagicMock(side_effect=AssertionError("signal mode must not audit an exchange"))
    monkeypatch.setattr(order_audit, "audit_grid_orders", audit)
    app = Flask(__name__)
    with app.test_request_context("/?id=3&sync=1"):
        g.user_id = 7
        result = inspect.unwrap(route.get_grid_resting_orders)().get_json()["data"]

    assert result["summary"]["virtual_mode"] is True
    assert result["summary"]["unverified_orders"] == 0
    assert result["orders"][0]["cell_index"] == 4
    assert result["orders"][0]["exchange_status"] == "open"
    assert result["orders"][0]["exchange_price"] == pytest.approx(95)
    assert result["orders"][0]["exchange_order_id"] == "virtual:8"
    audit.assert_not_called()
