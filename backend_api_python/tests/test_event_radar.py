from app.services import event_radar as module
from app.services.billing_config import load_billing_config
from app.services.search import SearchService
from app.services.search_models import SearchResponse, SearchResult


class FakeBilling:
    def __init__(self, *, accepted=True, message="consumed", cost=5):
        self.accepted = accepted
        self.message = message
        self.cost = cost
        self.refunds = []
        self.consumes = []

    def get_feature_cost(self, feature):
        assert feature == "event_radar"
        return self.cost

    def check_and_consume(self, user_id, feature, reference_id):
        assert user_id == 7
        assert feature == "event_radar"
        assert reference_id.startswith("event-radar:")
        self.consumes.append(reference_id)
        return self.accepted, self.message

    def get_user_credits(self, user_id):
        return 95

    def is_billing_enabled(self):
        return True

    def add_credits(self, **kwargs):
        self.refunds.append(kwargs)
        return True, "added"


class FakeLLM:
    def is_configured(self):
        return True

    def get_default_model(self):
        return "test-model"


def _settings():
    return {
        "enabled": True,
        "jev_min_confidence": 0.65,
        "news_lookback_days": 2,
        "max_events": 8,
        "crypto_rss_enabled": True,
        "yahoo_finance_rss_enabled": True,
        "sec_edgar_enabled": True,
        "sec_edgar_user_agent": "QuantDinger tests test@example.com",
    }


def test_event_radar_defaults_to_five_credits(monkeypatch):
    monkeypatch.delenv("BILLING_COST_EVENT_RADAR", raising=False)
    assert load_billing_config()["cost_event_radar"] == 5


def test_source_upgrade_reanalysis_is_free(monkeypatch):
    service = module.EventRadarService()
    billing = FakeBilling()
    monkeypatch.setattr(module, "get_billing_service", lambda: billing)
    monkeypatch.setattr(service, "_settings", _settings)
    monkeypatch.setattr(service, "_latest", lambda *args, **kwargs: {
        "source_status": {"pipeline_version": "localized_events_v3"},
    })
    monkeypatch.setattr(service, "_collect_events", lambda *args, **kwargs: (
        [{"kind": "news", "title": "Microsoft company update"}],
        {"pipeline_version": module.EVENT_RADAR_PIPELINE_VERSION},
    ))
    monkeypatch.setattr(service, "_jev_config", lambda threshold: {"api_key": "", "min_confidence": threshold})
    monkeypatch.setattr(module, "LLMService", FakeLLM)
    monkeypatch.setattr(service, "_analyze_llm", lambda *args, **kwargs: {
        "direction": "neutral",
        "confidence": 0.7,
        "impact": "medium",
        "relevance": "high",
        "freshness": "fresh",
        "summary": "Company news is available.",
        "provider": "llm",
        "model": "test-model",
    })
    monkeypatch.setattr(service, "_persist", lambda *args, **kwargs: None)

    result = service.analyze(7, "MSFT", "USStock")

    assert billing.consumes == []
    assert result["billing"]["charged"] == 0
    assert result["billing"]["status"] == "source_upgrade"


def test_event_radar_search_prefers_free_providers():
    calls = []

    class Provider:
        is_available = True

        def __init__(self, name, success):
            self.name = name
            self.success = success

        def search(self, query, max_results, days):
            calls.append(self.name)
            results = [SearchResult(title="Event", snippet="", url="https://example.test", source=self.name)] if self.success else []
            return SearchResponse(query=query, results=results, provider=self.name, success=self.success)

    service = SearchService.__new__(SearchService)
    service._providers = [Provider("Tavily", True), Provider("DuckDuckGo", True), Provider("GDELT", False)]

    response = service.search_free_first("BTC news", 5, 2)

    assert response.provider == "DuckDuckGo"
    assert calls == ["GDELT", "DuckDuckGo"]


def test_event_radar_search_continues_after_provider_exception():
    calls = []

    class Provider:
        is_available = True

        def __init__(self, name, raises=False):
            self.name = name
            self.raises = raises

        def search(self, query, max_results, days):
            calls.append(self.name)
            if self.raises:
                raise RuntimeError("provider down")
            return SearchResponse(
                query=query,
                results=[SearchResult(title="Bitcoin Event", snippet="", url="https://example.test", source=self.name)],
                provider=self.name,
                success=True,
            )

    service = SearchService.__new__(SearchService)
    service._providers = [Provider("GDELT", raises=True), Provider("DuckDuckGo")]

    response = service.search_free_first("BTC news", 5, 2)

    assert response.provider == "DuckDuckGo"
    assert calls == ["GDELT", "DuckDuckGo"]


def test_event_radar_search_skips_provider_with_only_unrelated_results():
    calls = []

    class Provider:
        is_available = True

        def __init__(self, name, title):
            self.name = name
            self.title = title

        def search(self, query, max_results, days):
            calls.append(self.name)
            return SearchResponse(
                query=query,
                results=[SearchResult(self.title, "", "https://example.test", self.name)],
                provider=self.name,
                success=True,
            )

    service = SearchService.__new__(SearchService)
    service._providers = [
        Provider("GDELT", "General market outlook"),
        Provider("DuckDuckGo", "Microsoft launches a new cloud service"),
    ]

    response = service.search_free_first(
        "Microsoft MSFT stock news",
        5,
        2,
        result_filter=lambda result: "microsoft" in result.title.lower(),
    )

    assert response.provider == "DuckDuckGo"
    assert calls == ["GDELT", "DuckDuckGo"]


def test_btc_collection_filters_unrelated_news_and_generic_macro(monkeypatch):
    service = module.EventRadarService()

    class Search:
        def search_free_first(self, query, max_results, days, result_filter=None):
            return SearchResponse(
                query=query,
                provider="GDELT",
                success=True,
                results=[
                    SearchResult("Bitcoin ETF inflows rise", "BTC demand improves", "https://news.test/btc", "GDELT"),
                    SearchResult("Meta developer conference", "New AI products", "https://news.test/meta", "GDELT"),
                ],
            )

    monkeypatch.setattr(module, "get_search_service", lambda: Search())
    monkeypatch.setattr(module, "fetch_coindesk_events", lambda **kwargs: [
        {"kind": "news", "title": "Bitcoin miners expand", "summary": "BTC mining update", "source": "CoinDesk RSS", "url": "https://rss.test/btc", "published_at": "2026-09-25"},
        {"kind": "news", "title": "Ethereum upgrade", "summary": "ETH only", "source": "CoinDesk RSS", "url": "https://rss.test/eth", "published_at": "2026-09-25"},
    ])
    monkeypatch.setattr(module, "get_economic_calendar_payload", lambda: {
        "source": "calendar",
        "status": "ok",
        "events": [
            {"event": "Meta developer conference", "importance": "high", "ai_insight": {"event_type": "macro", "affected_assets": [{"symbol": "BTC"}]}},
            {
                "event": "美国消费者价格指数",
                "name_en": "US Consumer Price Index",
                "actual": "2.8%",
                "forecast": "2.9%",
                "previous": "3.0%",
                "importance": "high",
                "ai_insight": {
                    "event_type": "inflation",
                    "summary": "美国通胀数据发布",
                    "summary_en": "US inflation data release",
                    "affected_assets": [{"symbol": "BTC"}],
                },
            },
        ],
    })

    events, status = service._collect_events("BTC/USDT", "swap", days=2, max_events=8)
    titles = [item["title"] for item in events]

    assert titles == ["Bitcoin miners expand", "Bitcoin ETF inflows rise", "美国消费者价格指数"]
    assert status["crypto_rss"]["relevant_count"] == 1
    assert status["news"]["relevant_count"] == 1
    assert status["calendar"]["relevant_count"] == 1
    macro_event = next(item for item in events if item["kind"] == "macro")
    assert macro_event["title_en"] == "US Consumer Price Index"
    assert macro_event["summary_en"] == "US inflation data release"
    assert macro_event["event_type"] == "inflation"
    assert macro_event["actual"] == "2.8%"


def test_us_stock_collection_prioritizes_ticker_scoped_news(monkeypatch):
    service = module.EventRadarService()
    queries = []

    class Search:
        def search_free_first(self, query, max_results, days, result_filter=None):
            queries.append(query)
            return SearchResponse(query=query, provider="None", success=False, results=[])

    monkeypatch.setattr(module, "get_seed_symbol_name", lambda market, symbol: "Microsoft Corporation")
    monkeypatch.setattr(module, "get_search_service", lambda: Search())
    monkeypatch.setattr(module, "fetch_yahoo_finance_events", lambda *args, **kwargs: [
        {
            "kind": "news",
            "title": "Microsoft expands its AI infrastructure",
            "summary": "The company announced new capacity.",
            "source": "Yahoo Finance RSS",
            "url": "https://finance.yahoo.com/news/msft-ai",
            "published_at": "2026-09-25",
        },
        {
            "kind": "news",
            "title": "Unrelated automaker announces a new model",
            "summary": "The automaker discussed production targets.",
            "source": "Yahoo Finance RSS",
            "url": "https://finance.yahoo.com/news/unrelated-auto",
            "published_at": "2026-09-25",
        },
    ])
    monkeypatch.setattr(module, "fetch_sec_filings", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "get_economic_calendar_payload", lambda: {"events": []})

    events, status = service._collect_events(
        "MSFT",
        "USStock",
        days=2,
        max_events=8,
        yahoo_finance_rss_enabled=True,
        sec_edgar_user_agent="QuantDinger tests test@example.com",
    )

    assert events[0]["title"] == "Microsoft expands its AI infrastructure"
    assert len(events) == 1
    assert status["stock_rss"]["count"] == 2
    assert status["stock_rss"]["relevant_count"] == 1
    assert "Microsoft Corporation" in queries[0]


def test_event_radar_falls_back_to_llm_and_is_reference_only(monkeypatch):
    service = module.EventRadarService()
    billing = FakeBilling()
    saved = []
    monkeypatch.setattr(module, "get_billing_service", lambda: billing)
    monkeypatch.setattr(service, "_settings", _settings)
    monkeypatch.setattr(service, "_collect_events", lambda *args, **kwargs: ([{"kind": "news", "title": "Test"}], {"news": {"ok": True}}))
    monkeypatch.setattr(service, "_jev_config", lambda threshold: {"api_key": "key", "min_confidence": threshold})
    monkeypatch.setattr(service, "_analyze_jev", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("low confidence")))
    monkeypatch.setattr(module, "LLMService", FakeLLM)
    monkeypatch.setattr(service, "_analyze_llm", lambda *args, **kwargs: {
        "direction": "bearish",
        "confidence": 0.74,
        "impact": "high",
        "relevance": "high",
        "freshness": "fresh",
        "summary": "Current evidence is bearish.",
        "provider": "llm",
        "model": "test-model",
    })
    monkeypatch.setattr(service, "_persist", lambda user_id, payload: saved.append((user_id, payload)))

    result = service.analyze(7, "BTC/USDT", "swap")

    assert result["direction"] == "bearish"
    assert result["provider"] == "llm"
    assert result["reference_only"] is True
    assert result["billing"]["charged"] == 5
    assert "jev:low confidence" in result["fallback_reason"]
    assert saved and saved[0][0] == 7


def test_legacy_macro_titles_are_classified_without_reanalysis():
    assert module.EventRadarService._legacy_macro_type("美国首次申请失业救济人数") == "jobs"
    assert module.EventRadarService._legacy_macro_type("FOMC voting member speaks") == "central_bank"
    assert module.EventRadarService._legacy_macro_type("US CPI") == "inflation"


def test_event_radar_refunds_when_all_models_fail(monkeypatch):
    service = module.EventRadarService()
    billing = FakeBilling()
    monkeypatch.setattr(module, "get_billing_service", lambda: billing)
    monkeypatch.setattr(service, "_settings", _settings)
    monkeypatch.setattr(service, "_collect_events", lambda *args, **kwargs: ([{"kind": "news", "title": "Test"}], {}))
    monkeypatch.setattr(service, "_jev_config", lambda threshold: {"api_key": "key", "min_confidence": threshold})
    monkeypatch.setattr(service, "_analyze_jev", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("jev down")))

    class BrokenLLM:
        def is_configured(self):
            return True

    monkeypatch.setattr(module, "LLMService", BrokenLLM)
    monkeypatch.setattr(service, "_analyze_llm", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm down")))

    try:
        service.analyze(7, "BTC/USDT", "swap")
        raise AssertionError("expected EventRadarError")
    except module.EventRadarError as exc:
        assert exc.code == "event_radar_provider_unavailable"
        assert exc.details["billing"]["status"] == "refunded"

    assert billing.refunds[0]["amount"] == 5


def test_event_radar_empty_evidence_returns_neutral_without_models(monkeypatch):
    service = module.EventRadarService()
    billing = FakeBilling()
    saved = []
    monkeypatch.setattr(module, "get_billing_service", lambda: billing)
    monkeypatch.setattr(service, "_settings", _settings)
    monkeypatch.setattr(service, "_collect_events", lambda *args, **kwargs: ([], {"news": {"ok": False}}))
    monkeypatch.setattr(service, "_analyze_jev", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("JEV must not run")))
    monkeypatch.setattr(service, "_analyze_llm", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run")))
    monkeypatch.setattr(service, "_persist", lambda user_id, payload: saved.append(payload))

    result = service.analyze(7, "BTC/USDT", "swap")

    assert result["direction"] == "neutral"
    assert result["confidence"] == 0
    assert result["relevance"] == "low"
    assert result["provider"] == "evidence_gate"
    assert result["billing"]["charged"] == 5
    assert "evidence:no_relevant_events" in result["fallback_reason"]
    assert saved


def test_event_radar_disabled_does_not_charge(monkeypatch):
    service = module.EventRadarService()
    billing = FakeBilling()
    monkeypatch.setattr(module, "get_billing_service", lambda: billing)
    monkeypatch.setattr(service, "_settings", lambda: {**_settings(), "enabled": False})

    try:
        service.analyze(7, "BTC/USDT", "swap")
        raise AssertionError("expected EventRadarError")
    except module.EventRadarError as exc:
        assert exc.code == "event_radar_disabled"
        assert exc.status == 403
