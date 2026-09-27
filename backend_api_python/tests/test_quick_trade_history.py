import inspect
from contextlib import contextmanager

from flask import Flask, g

from app.routes import quick_trade, quick_trade_event_radar


class _Cursor:
    def __init__(self):
        self.query = ""
        self.params = ()

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        return [{
            "id": 7,
            "credential_id": 305,
            "exchange_id": "okx",
            "symbol": "BTC/USDT",
            "side": "buy",
            "order_type": "market",
            "amount": 100,
            "price": 0,
            "leverage": 5,
            "market_type": "swap",
            "status": "filled",
            "exchange_order_id": "order-1",
            "filled_amount": 0.001,
            "avg_fill_price": 75000,
            "commission": 0.03,
            "commission_ccy": "USDT",
            "commission_quote": 0.03,
            "raw_result": {
                "_quick_trade": {
                    "margin_mode": "cross",
                    "requested_base_qty": 0.0012,
                    "notional_usdt": 500,
                    "amount_semantics": "margin",
                    "client_order_id": "qd-quick-7",
                }
            },
        }]

    def close(self):
        return None


class _Db:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _InsertCursor(_Cursor):
    def fetchone(self):
        return {"id": 17}


class _WritableDb(_Db):
    def __init__(self, cursor):
        super().__init__(cursor)
        self.committed = False

    def commit(self):
        self.committed = True


def test_record_quick_trade_persists_client_order_id(monkeypatch):
    cursor = _InsertCursor()
    database = _WritableDb(cursor)

    @contextmanager
    def fake_connection():
        yield database

    monkeypatch.setattr(quick_trade, "get_db_connection", fake_connection)

    trade_id = quick_trade._record_quick_trade(
        user_id=99,
        credential_id=305,
        exchange_id="okx",
        symbol="BTC/USDT",
        side="buy",
        order_type="market",
        amount=0.01,
        price=0,
        leverage=1,
        market_type="swap",
        tp_price=0,
        sl_price=0,
        status="submitted",
        exchange_order_id="",
        filled=0,
        avg_price=0,
        error_msg="",
        source="quick_trade",
        raw_result={},
        client_order_id="client-17",
    )

    assert trade_id == 17
    assert database.committed is True
    assert "client_order_id" in cursor.query
    assert cursor.params[-4] == "client-17"


def test_history_filters_by_account_symbol_and_market(monkeypatch):
    cursor = _Cursor()

    @contextmanager
    def fake_connection():
        yield _Db(cursor)

    monkeypatch.setattr(quick_trade, "get_db_connection", fake_connection)
    app = Flask(__name__)
    handler = inspect.unwrap(quick_trade.get_history)

    with app.test_request_context(
        "/api/quick-trade/history?limit=20&credential_id=305&symbol=BTC/USDT&market_type=perpetual"
    ):
        g.user_id = 99
        response = handler()

    payload = response.get_json()
    assert "credential_id = %s" in cursor.query
    assert "UPPER(symbol) = UPPER(%s)" in cursor.query
    assert "market_type = %s" in cursor.query
    assert cursor.params == (99, 305, "BTC/USDT", "swap", 20, 0)
    assert payload["data"]["trades"][0]["credential_id"] == 305
    assert payload["data"]["trades"][0]["commission_quote"] == 0.03
    assert payload["data"]["trades"][0]["margin_mode"] == "cross"
    assert payload["data"]["trades"][0]["requested_base_qty"] == 0.0012
    assert payload["data"]["trades"][0]["client_order_id"] == "qd-quick-7"


def test_ai_decision_history_is_scoped_to_selected_account(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return [{"decision_uid": "decision-1", "decision": "pass"}]

    monkeypatch.setattr(quick_trade, "list_ai_decisions", fake_list)
    app = Flask(__name__)
    handler = inspect.unwrap(quick_trade.get_ai_decisions)

    with app.test_request_context(
        "/api/quick-trade/ai-decisions?credential_id=305&symbol=BTC/USDT&market_type=swap&limit=25"
    ):
        g.user_id = 99
        response = handler()

    assert captured == {
        "user_id": 99,
        "source_type": "quick_trade",
        "source_id": 305,
        "symbol": "BTC/USDT",
        "market_type": "swap",
        "limit": 25,
    }
    assert response.get_json()["data"][0]["decision_uid"] == "decision-1"


def test_event_radar_status_is_scoped_to_current_user(monkeypatch):
    captured = {}

    class Service:
        def get_status(self, user_id, symbol, market_type):
            captured.update(user_id=user_id, symbol=symbol, market_type=market_type)
            return {"enabled": True, "cost": 5, "latest": None}

    monkeypatch.setattr(quick_trade_event_radar, "get_event_radar_service", lambda: Service())
    app = Flask(__name__)
    handler = inspect.unwrap(quick_trade_event_radar.get_event_radar)

    with app.test_request_context("/api/quick-trade/event-radar?symbol=BTC/USDT&market_type=swap"):
        g.user_id = 99
        response = handler()

    assert response.get_json()["data"]["cost"] == 5
    assert captured == {"user_id": 99, "symbol": "BTC/USDT", "market_type": "swap"}


def test_event_radar_analysis_never_calls_order_execution(monkeypatch):
    class Service:
        def analyze(self, user_id, symbol, market_type):
            return {"reference_only": True, "symbol": symbol, "market_type": market_type}

    monkeypatch.setattr(quick_trade_event_radar, "get_event_radar_service", lambda: Service())
    app = Flask(__name__)
    handler = inspect.unwrap(quick_trade_event_radar.analyze_event_radar)
    assert "place_order" not in inspect.getsource(handler)

    with app.test_request_context(
        "/api/quick-trade/event-radar/analyze",
        method="POST",
        json={"symbol": "BTC/USDT", "market_type": "swap"},
    ):
        g.user_id = 99
        response = handler()

    assert response.get_json()["data"]["reference_only"] is True
