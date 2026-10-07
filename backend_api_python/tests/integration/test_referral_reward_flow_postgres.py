import os
import re
import uuid

import pytest

from app.services.billing_service import BillingService
from app.services.referral_reward_service import ReferralRewardService
import app.services.billing_service as billing_service_module
import app.services.referral_reward_service as referral_reward_module


class _CursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("?", "%s"), params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()


class _ConnectionAdapter:
    def __init__(self, dsn, schema):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        self._raw = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
        with self._raw.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        self._raw.commit()

    def cursor(self):
        return _CursorAdapter(self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self._raw.rollback()
        self._raw.close()


@pytest.fixture
def isolated_postgres_schema():
    dsn = os.getenv("QD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("QD_TEST_POSTGRES_DSN is required")
    import psycopg2

    schema = f"qd_referral_test_{uuid.uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", schema)
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(
                f"""
                CREATE TABLE "{schema}".qd_users (
                  id INTEGER PRIMARY KEY,
                  username VARCHAR(80) NOT NULL UNIQUE,
                  nickname VARCHAR(120) NOT NULL DEFAULT '',
                  referred_by INTEGER REFERENCES "{schema}".qd_users(id) ON DELETE SET NULL
                )
                """
            )
        yield lambda: _ConnectionAdapter(dsn, schema)
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


@pytest.mark.integration
def test_referral_reward_withdrawal_lifecycle(isolated_postgres_schema, monkeypatch):
    connect = isolated_postgres_schema
    monkeypatch.setattr(referral_reward_module, "get_db_connection", connect)
    monkeypatch.setenv("REFERRAL_PROGRAM_ENABLED", "true")
    monkeypatch.setenv("REFERRAL_REWARD_RATES", "10,5,3")
    monkeypatch.setenv("REFERRAL_REWARD_HOLD_DAYS", "0")
    monkeypatch.setenv("REFERRAL_MIN_WITHDRAWAL_USD", "1")

    service = ReferralRewardService()
    monkeypatch.setattr(
        service,
        "_public_chains",
        lambda: [{"currency": "USDT", "code": "TRC20", "label": "TRC20"}],
    )
    with connect() as db:
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO qd_users (id, username, nickname, referred_by) VALUES
              (1, 'level3', 'Level 3', NULL),
              (2, 'level2', 'Level 2', 1),
              (3, 'level1', 'Level 1', 2),
              (4, 'buyer', 'Buyer', 3)
            """
        )
        plan = {"code": "vip", "price_usd": 100, "referral_eligible": True}
        service.award_membership_rewards(cursor, buyer_id=4, plan=plan, source_ref="integration-order-1")
        service.award_membership_rewards(cursor, buyer_id=4, plan=plan, source_ref="integration-order-1")
        db.commit()
        cursor.close()

    assert service.summary(3)["account"]["available_balance"] == 10.0
    assert service.summary(2)["account"]["available_balance"] == 5.0
    assert service.summary(1)["account"]["available_balance"] == 3.0

    ok, msg, first = service.create_withdrawal(
        3,
        {"currency": "USDT", "chain": "TRC20", "address": "T" + "A" * 33, "amount": 4},
    )
    assert (ok, msg) == (True, "success")
    after_request = service.summary(3)["account"]
    assert after_request["available_balance"] == 6.0
    assert after_request["pending_withdrawal_balance"] == 4.0

    assert service.review_withdrawal(first["withdrawal_id"], 1, {"action": "processing"})[:2] == (True, "success")
    assert service.review_withdrawal(
        first["withdrawal_id"],
        1,
        {"action": "paid", "tx_hash": "integration-payment-1"},
    )[:2] == (True, "success")

    ok, _, second = service.create_withdrawal(
        3,
        {"currency": "USDT", "chain": "TRC20", "address": "T" + "B" * 33, "amount": 2},
    )
    assert ok
    assert service.review_withdrawal(second["withdrawal_id"], 1, {"action": "rejected"})[:2] == (True, "success")

    final_summary = service.summary(3)
    assert final_summary["account"]["available_balance"] == 6.0
    assert final_summary["account"]["pending_withdrawal_balance"] == 0.0
    assert final_summary["account"]["lifetime_earned"] == 10.0
    assert final_summary["account"]["lifetime_paid"] == 4.0
    assert {row["action"] for row in final_summary["ledger"]} >= {
        "membership_reward",
        "withdrawal_requested",
        "withdrawal_paid",
        "withdrawal_rejected",
    }

    admin_view = service.list_withdrawals(page=1, page_size=20)
    assert admin_view["total"] == 2
    assert admin_view["summary"]["paid_requests"] == 1
    assert admin_view["summary"]["rejected_requests"] == 1
    assert admin_view["summary"]["paid_amount"] == 4.0
    assert admin_view["summary"]["pending_amount"] == 0.0


@pytest.mark.integration
def test_membership_plan_can_be_deleted(isolated_postgres_schema, monkeypatch):
    connect = isolated_postgres_schema
    monkeypatch.setattr(billing_service_module, "get_db_connection", connect)
    service = BillingService()
    plans = [
        {"code": "starter", "name": "Starter", "price_usd": 10, "duration_days": 30, "is_active": True},
        {"code": "pro", "name": "Pro", "price_usd": 20, "duration_days": 30, "is_active": True},
    ]
    assert service.save_membership_plans(plans)[:2] == (True, "success")
    with connect() as db:
        cursor = db.cursor()
        cursor.execute(
            "CREATE TABLE qd_billing_orders (id BIGSERIAL PRIMARY KEY, plan VARCHAR(64) NOT NULL)"
        )
        cursor.execute("INSERT INTO qd_billing_orders (plan) VALUES (?)", ("starter",))
        db.commit()
        cursor.close()
    assert service.delete_membership_plan("starter")[:2] == (True, "success")
    assert "starter" not in service.get_membership_plans(include_inactive=True)
    with connect() as db:
        cursor = db.cursor()
        cursor.execute("SELECT plan FROM qd_billing_orders")
        assert cursor.fetchone()["plan"] == "starter"
        cursor.close()
    assert service.delete_membership_plan("pro")[:2] == (False, "cannot_delete_last_active_plan")
