import json

from app.services.ai_authoring_intent import resolve_authoring_intent
from app.services.indicator_ai_workspace import classify_indicator_ai_intent


class FakeLLM:
    def __init__(self, response, *, configured=True):
        self.response = response
        self.configured = configured
        self.calls = []

    def is_configured(self):
        return self.configured

    def get_default_model(self):
        return "intent-test-model"

    def call_llm_api(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_auto_mode_uses_model_as_the_canonical_router():
    llm = FakeLLM('{"intent":"modify","confidence":0.94,"reason":"continues the prior plan"}')

    result = resolve_authoring_intent(
        prompt="就照前面的方案落地",
        asset_kind="chart_indicator",
        fallback_classifier=classify_indicator_ai_intent,
        llm=llm,
    )

    assert classify_indicator_ai_intent("就照前面的方案落地") == "discussion"
    assert result == {
        "intent": "modify",
        "confidence": 0.94,
        "reason": "continues the prior plan",
        "source": "model",
    }
    assert len(llm.calls) == 1
    assert llm.calls[0]["temperature"] == 0.0
    assert llm.calls[0]["use_json_mode"] is True
    assert llm.calls[0]["use_fallback"] is False
    assert llm.calls[0]["try_alternative_providers"] is False
    assert llm.calls[0]["timeout_seconds"] == 12.0


def test_model_can_route_a_keyword_false_positive_to_discussion():
    llm = FakeLLM({"intent": "discussion", "confidence": 88, "reason": "asks for a diagnosis"})

    result = resolve_authoring_intent(
        prompt="生成失败的原因是什么",
        asset_kind="cta_strategy",
        fallback_classifier=classify_indicator_ai_intent,
        llm=llm,
    )

    assert classify_indicator_ai_intent("生成失败的原因是什么") == "modify"
    assert result["intent"] == "discussion"
    assert result["confidence"] == 0.88
    assert result["source"] == "model"


def test_explicit_mode_bypasses_the_model_router():
    llm = FakeLLM(AssertionError("model must not be called"))

    result = resolve_authoring_intent(
        prompt="解释这个策略",
        requested_mode="modify",
        asset_kind="cta_strategy",
        fallback_classifier=classify_indicator_ai_intent,
        llm=llm,
    )

    assert result["intent"] == "modify"
    assert result["source"] == "explicit"
    assert llm.calls == []


def test_invalid_or_unavailable_model_falls_back_to_rules():
    invalid = FakeLLM("not-json")
    unavailable = FakeLLM("", configured=False)

    invalid_result = resolve_authoring_intent(
        prompt="把参数改成 20",
        asset_kind="chart_indicator",
        fallback_classifier=classify_indicator_ai_intent,
        llm=invalid,
    )
    unavailable_result = resolve_authoring_intent(
        prompt="为什么没有信号？",
        asset_kind="chart_indicator",
        fallback_classifier=classify_indicator_ai_intent,
        llm=unavailable,
    )

    assert invalid_result["intent"] == "modify"
    assert invalid_result["source"] == "fallback"
    assert unavailable_result["intent"] == "discussion"
    assert unavailable_result["source"] == "fallback"


def test_router_uses_recent_context_without_prior_assistant_discussion():
    llm = FakeLLM('{"intent":"modify","confidence":1,"reason":"approved implementation"}')

    resolve_authoring_intent(
        prompt="继续",
        asset_kind="portfolio_strategy",
        existing_code="def initialize(context):\n    pass",
        recent_messages=[
            {"role": "user", "content": "给我写一个轮动策略"},
            {
                "role": "assistant",
                "content": "请再确认一次",
                "message_type": "discussion",
            },
            {
                "role": "assistant",
                "content": "Candidate generated",
                "message_type": "candidate",
            },
        ],
        fallback_classifier=classify_indicator_ai_intent,
        llm=llm,
    )

    payload = json.loads(llm.calls[0]["messages"][1]["content"])
    assert payload["editor_has_source"] is True
    assert payload["current_user_turn"] == "继续"
    assert payload["recent_conversation"] == [
        {"role": "user", "content": "给我写一个轮动策略"},
        {"role": "assistant", "content": "Candidate generated"},
    ]
