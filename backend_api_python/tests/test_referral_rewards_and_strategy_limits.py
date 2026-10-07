from decimal import Decimal

import pytest

from app.services.billing_service import BillingService
from app.services.referral_reward_service import ReferralRewardService


def test_referral_rates_accept_percent_notation(monkeypatch):
    monkeypatch.setenv("REFERRAL_REWARD_RATES", "10%, 5, 3")
    assert ReferralRewardService().rates() == [Decimal("10"), Decimal("5"), Decimal("3")]


def test_referral_rates_reject_more_than_three_levels(monkeypatch):
    monkeypatch.setenv("REFERRAL_REWARD_RATES", "10,5,3,1")
    assert ReferralRewardService().rates() == []


def test_referral_rates_reject_invalid_total(monkeypatch):
    monkeypatch.setenv("REFERRAL_REWARD_RATES", "60,50")
    assert ReferralRewardService().rates() == []


def test_withdrawal_address_validation_covers_enabled_network_shapes():
    validate = ReferralRewardService._valid_address
    assert validate("ERC20", "0x" + "a" * 40)
    assert validate("BEP20", "0x" + "1" * 40)
    assert validate("TRC20", "T" + "A" * 33)
    assert validate("SOL", "A" * 32)
    assert not validate("ERC20", "T" + "A" * 33)
    assert not validate("SOL", "0x" + "a" * 40)


@pytest.mark.parametrize(
    ("usdt_enabled", "usdc_enabled", "expected"),
    [
        (True, False, {"USDT"}),
        (False, True, {"USDC"}),
        (True, True, {"USDT", "USDC"}),
    ],
)
def test_withdrawal_currencies_follow_enabled_payment_methods(monkeypatch, usdt_enabled, usdc_enabled, expected):
    monkeypatch.setenv("USDT_PAY_ENABLED", str(usdt_enabled))
    monkeypatch.setenv("USDC_PAY_ENABLED", str(usdc_enabled))
    monkeypatch.setenv("USDT_PAY_ENABLED_CHAINS", "ERC20")
    monkeypatch.setenv("USDC_PAY_ENABLED_CHAINS", "ERC20")
    monkeypatch.setenv("USDT_ERC20_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("USDC_ERC20_ADDRESS", "0x" + "2" * 40)

    channels = ReferralRewardService._public_chains()

    assert {channel["currency"] for channel in channels} == expected
    assert all(channel["code"] == "ERC20" for channel in channels)


def test_plan_serialization_includes_strategy_and_referral_policy():
    out = BillingService._serialize_plan({
        "code": "pro",
        "strategy_limit": 12,
        "referral_eligible": True,
    })
    assert out["strategy_limit"] == 12
    assert out["referral_eligible"] is True


def test_plan_serialization_has_safe_strategy_default():
    out = BillingService._serialize_plan({"code": "legacy"})
    assert out["strategy_limit"] == 10
    assert out["referral_eligible"] is False
