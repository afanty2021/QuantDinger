from __future__ import annotations

from contextlib import AbstractContextManager

import pytest

from app.services.execution_streams import processor as processor_module


class FakeCursor:
    def __init__(self, calls, pending):
        self.calls = calls
        self.pending = pending

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.pending

    def close(self):
        return None


class FakeConnection(AbstractContextManager):
    def __init__(self, calls, pending):
        self.calls = calls
        self.pending = pending

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return FakeCursor(self.calls, self.pending)

    def commit(self):
        return None


@pytest.mark.parametrize(
    ("order_status", "queue_status", "intent_status"),
    [("filled", "filled", "filled"), ("open", "sent", "partially_filled")],
)
def test_late_fill_recovers_failed_queue_and_intent(
    monkeypatch,
    order_status,
    queue_status,
    intent_status,
):
    calls = []
    pending = {
        "id": 7,
        "strategy_id": 0,
        "order_intent_id": 9,
        "filled": 0,
        "avg_price": 0,
        "payload_json": "{}",
    }
    monkeypatch.setattr(
        processor_module,
        "get_db_connection",
        lambda: FakeConnection(calls, pending),
    )
    monkeypatch.setattr(
        processor_module.ExecutionEventProcessor,
        "_already_projected",
        staticmethod(lambda _event_id: False),
    )
    monkeypatch.setattr(
        processor_module,
        "posted_totals",
        lambda *_args: {"quantity": 0.0, "average": 0.0},
    )
    monkeypatch.setattr(
        processor_module,
        "combine_pending_snapshot",
        lambda event, _pending: (event, {}),
    )
    event = {
        "id": 101,
        "quantity": 1.0,
        "cumulative_quantity": 1.0,
        "is_cumulative": True,
        "price": 100.0,
        "order_status": order_status,
        "fee_status": "actual",
        "exchange_order_id": "exchange-101",
    }
    binding = {"id": 8, "pending_order_id": 7, "strategy_id": 0}

    processor_module.ExecutionEventProcessor()._project_pending_order(event, binding)

    pending_update = next(item for item in calls if "UPDATE pending_orders" in item[0])
    assert "status = 'failed' AND %s > 0" in pending_update[0]
    assert pending_update[1][3:5] == (queue_status, 1.0)
    intent_update = next(item for item in calls if "UPDATE strategy_order_intents" in item[0])
    assert intent_update[1] == (queue_status, 1.0, queue_status, 9)
    assert intent_status in intent_update[0]
