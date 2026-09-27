"""Reference-only event analysis for the Quick Trade workspace."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

from app.data_providers.economic_calendar import get_economic_calendar_payload
from app.data.market_symbols_seed import get_symbol_name as get_seed_symbol_name
from app.data_providers.event_sources import fetch_coindesk_events, fetch_sec_filings, fetch_yahoo_finance_events
from app.services.billing_service import get_billing_service
from app.services.llm import LLMService
from app.services.search import get_search_service
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)

JEV_QUESTIONS = {
    "relevance": {
        "type": "choice",
        "instructions": "Assess whether the supplied current events are materially relevant to the requested instrument.",
        "criteria": {
            "high": "Several fresh events have a direct and credible connection to the instrument.",
            "medium": "At least one event is relevant, but the evidence is indirect or mixed.",
            "low": "The supplied events have little credible connection to the instrument.",
        },
    },
    "direction": {
        "type": "choice",
        "instructions": "Estimate the near-term directional effect of the supplied events on the instrument.",
        "criteria": {
            "bullish": "The event evidence is more likely to support price appreciation.",
            "bearish": "The event evidence is more likely to support price depreciation.",
            "neutral": "The evidence has no material directional effect.",
            "mixed": "Material bullish and bearish effects coexist.",
        },
    },
    "impact": {
        "type": "choice",
        "instructions": "Estimate the likely market impact of these events over the next trading session.",
        "criteria": {
            "high": "The evidence can plausibly produce a large or abrupt price response.",
            "medium": "The evidence may cause a visible but ordinary price response.",
            "low": "The evidence is unlikely to move the instrument materially.",
        },
    },
    "freshness": {
        "type": "choice",
        "instructions": "Assess whether the evidence is current enough for a trading reference now.",
        "criteria": {
            "fresh": "Most evidence is current and timely.",
            "mixed": "Evidence includes both current and older items.",
            "stale": "Most evidence is too old to guide a current decision.",
        },
    },
}

JEV_OPTIONS = {
    "relevance": {"high", "medium", "low"},
    "direction": {"bullish", "bearish", "neutral", "mixed"},
    "impact": {"high", "medium", "low"},
    "freshness": {"fresh", "mixed", "stale"},
}

CRYPTO_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "BNB": "BNB",
    "XRP": "XRP",
    "DOGE": "Dogecoin",
}

EVENT_RADAR_PIPELINE_VERSION = "instrument_news_v4"


class EventRadarError(Exception):
    def __init__(self, code: str, *, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


class EventRadarService:
    """Collect free event data and classify it with JEV-first fallback."""

    def get_status(self, user_id: int, symbol: str = "", market_type: str = "") -> dict[str, Any]:
        settings = self._settings()
        latest = self._latest(user_id, symbol, market_type)
        billing = get_billing_service()
        return {
            "enabled": settings["enabled"],
            "cost": max(0, int(billing.get_feature_cost("event_radar") or 0)),
            "billing_enabled": billing.is_billing_enabled(),
            "latest": latest,
            "upgrade_available": bool(
                latest
                and latest.get("source_status", {}).get("pipeline_version") != EVENT_RADAR_PIPELINE_VERSION
            ),
        }

    def analyze(self, user_id: int, symbol: str, market_type: str) -> dict[str, Any]:
        settings = self._settings()
        if not settings["enabled"]:
            raise EventRadarError("event_radar_disabled", status=403)

        normalized_symbol = self._normalize_symbol(symbol)
        if not normalized_symbol:
            raise EventRadarError("event_radar_symbol_required")

        analysis_id = str(uuid.uuid4())
        reference_id = f"event-radar:{analysis_id}"
        billing = get_billing_service()
        cost = max(0, int(billing.get_feature_cost("event_radar") or 0))
        previous = self._latest(user_id, normalized_symbol, market_type)
        source_upgrade = bool(
            previous
            and previous.get("source_status", {}).get("pipeline_version") != EVENT_RADAR_PIPELINE_VERSION
        )
        if source_upgrade:
            accepted, message = True, "source_upgrade"
        else:
            accepted, message = billing.check_and_consume(user_id, "event_radar", reference_id)
        receipt = {
            "feature": "event_radar",
            "reference_id": reference_id,
            "cost": cost,
            "charged": cost if accepted and message == "consumed" else 0,
            "refunded": 0,
            "status": "source_upgrade" if source_upgrade else ("charged" if message == "consumed" else "free"),
            "message": str(message or ""),
        }
        if not accepted:
            code = "insufficient_credits" if str(message).startswith("insufficient_credits") else "billing_unavailable"
            raise EventRadarError(code, status=402 if code == "insufficient_credits" else 503, details={"billing": receipt})

        started = time.perf_counter()
        events, source_status = self._collect_events(
            normalized_symbol,
            market_type,
            days=settings["news_lookback_days"],
            max_events=settings["max_events"],
            crypto_rss_enabled=settings["crypto_rss_enabled"],
            yahoo_finance_rss_enabled=settings["yahoo_finance_rss_enabled"],
            sec_edgar_enabled=settings["sec_edgar_enabled"],
            sec_edgar_user_agent=settings["sec_edgar_user_agent"],
        )
        failures: list[str] = []
        result: dict[str, Any] | None = None
        if not events:
            failures.append("evidence:no_relevant_events")
            result = {
                "direction": "neutral",
                "confidence": 0.0,
                "impact": "low",
                "relevance": "low",
                "freshness": "fresh",
                "summary": "",
                "provider": "evidence_gate",
                "model": "rules-v1",
            }
        else:
            jev = self._jev_config(settings["jev_min_confidence"])
            if jev["api_key"]:
                try:
                    result = self._analyze_jev(normalized_symbol, market_type, events, jev)
                except Exception as exc:
                    failures.append(f"jev:{self._safe_error(exc)}")
                    logger.warning("Event Radar JEV analysis failed: %s", exc)

            if result is None:
                try:
                    llm = LLMService()
                    if llm.is_configured():
                        result = self._analyze_llm(normalized_symbol, market_type, events, llm)
                    else:
                        failures.append("llm:not_configured")
                except Exception as exc:
                    failures.append(f"llm:{self._safe_error(exc)}")
                    logger.warning("Event Radar LLM analysis failed: %s", exc)

        if result is None:
            receipt = self._refund(user_id, receipt)
            raise EventRadarError(
                "event_radar_provider_unavailable",
                status=503,
                details={"billing": receipt, "fallback_reason": "; ".join(failures)},
            )

        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "analysis_id": analysis_id,
            "symbol": normalized_symbol,
            "market_type": str(market_type or "").strip(),
            "direction": result["direction"],
            "confidence": result["confidence"],
            "impact": result["impact"],
            "relevance": result["relevance"],
            "freshness": result["freshness"],
            "summary": result.get("summary") or "",
            "provider": result["provider"],
            "model": result.get("model") or "",
            "fallback_reason": "; ".join(failures),
            "source_status": source_status,
            "events": events,
            "billing": {**receipt, "remaining": float(billing.get_user_credits(user_id))},
            "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "created_at": created_at,
            "reference_only": True,
        }
        self._persist(user_id, payload)
        return payload

    def _collect_events(
        self,
        symbol: str,
        market_type: str,
        *,
        days: int,
        max_events: int,
        crypto_rss_enabled: bool = True,
        yahoo_finance_rss_enabled: bool = True,
        sec_edgar_enabled: bool = True,
        sec_edgar_user_agent: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        normalized = str(symbol or "").rsplit(":", 1)[-1]
        base = normalized.split("/")[0].split("-")[0].upper()
        is_crypto = str(market_type or "").lower() not in {"usstock", "stock", "stocks", "equity"}
        asset_name = CRYPTO_NAMES.get(base, base) if is_crypto else (get_seed_symbol_name("USStock", base) or base)
        query = f'("{asset_name}" OR "{base}") {"crypto" if is_crypto else "stock"} news market'
        source_status: dict[str, Any] = {"pipeline_version": EVENT_RADAR_PIPELINE_VERSION}
        direct_events: list[dict[str, Any]] = []
        seen: set[str] = set()

        if is_crypto and crypto_rss_enabled:
            try:
                rss_events = fetch_coindesk_events(days=days, limit=max_events)
                for item in rss_events:
                    if self._news_relevant(item, base, asset_name, is_crypto=True):
                        self._append_unique(direct_events, item, seen)
                source_status["crypto_rss"] = {
                    "provider": "CoinDesk RSS", "ok": True, "count": len(rss_events),
                    "relevant_count": len(direct_events),
                }
            except Exception as exc:
                logger.warning("Event Radar crypto RSS collection failed: %s", exc)
                source_status["crypto_rss"] = {"provider": "CoinDesk RSS", "ok": False, "count": 0}

        if not is_crypto and yahoo_finance_rss_enabled:
            try:
                stock_rss_events = fetch_yahoo_finance_events(base, days=days, limit=max_events)
                for item in stock_rss_events:
                    if self._news_relevant(item, base, asset_name, is_crypto=False):
                        self._append_unique(direct_events, item, seen)
                source_status["stock_rss"] = {
                    "provider": "Yahoo Finance RSS", "ok": True, "count": len(stock_rss_events),
                    "relevant_count": len(direct_events),
                }
            except Exception as exc:
                logger.warning("Event Radar stock RSS collection failed: %s", exc)
                source_status["stock_rss"] = {"provider": "Yahoo Finance RSS", "ok": False, "count": 0}

        try:
            news_response = get_search_service().search_free_first(
                query,
                max_events,
                days,
                result_filter=lambda result: self._news_relevant(
                    {"title": result.title, "summary": result.snippet},
                    base,
                    asset_name,
                    is_crypto=is_crypto,
                ),
            )
            news = news_response.to_list() if news_response.success else []
            provider = news_response.provider
            search_ok = bool(news_response.success)
        except Exception as exc:
            logger.warning("Event Radar instrument news collection failed: %s", exc)
            news, provider, search_ok = [], "None", False
        relevant_search_count = 0
        for item in news:
            normalized_item = {
                "kind": "news",
                "title": str(item.get("title") or "").strip()[:300],
                "summary": str(item.get("snippet") or item.get("summary") or "")[:600],
                "source": str(item.get("source") or provider or ""),
                "url": str(item.get("link") or item.get("url") or "").strip(),
                "published_at": str(item.get("published") or item.get("published_at") or ""),
            }
            if self._news_relevant(normalized_item, base, asset_name, is_crypto=is_crypto):
                before = len(direct_events)
                self._append_unique(direct_events, normalized_item, seen)
                relevant_search_count += int(len(direct_events) > before)
        source_status["news"] = {
            "provider": provider, "ok": search_ok, "count": len(news),
            "relevant_count": relevant_search_count,
        }

        if not is_crypto and sec_edgar_enabled:
            if sec_edgar_user_agent:
                try:
                    filings = fetch_sec_filings(
                        base, days=days, limit=max_events, user_agent=sec_edgar_user_agent,
                    )
                    for item in filings:
                        self._append_unique(direct_events, item, seen)
                    source_status["sec_edgar"] = {
                        "provider": "SEC EDGAR", "ok": True, "count": len(filings),
                    }
                except Exception as exc:
                    logger.warning("Event Radar SEC EDGAR collection failed: %s", exc)
                    source_status["sec_edgar"] = {"provider": "SEC EDGAR", "ok": False, "count": 0}
            else:
                source_status["sec_edgar"] = {
                    "provider": "SEC EDGAR", "ok": False, "count": 0, "reason": "user_agent_missing",
                }

        try:
            calendar_payload = get_economic_calendar_payload()
        except Exception as exc:
            logger.warning("Event Radar economic calendar collection failed: %s", exc)
            calendar_payload = {}
        calendar = calendar_payload.get("events") if isinstance(calendar_payload, dict) else []
        macro_events: list[dict[str, Any]] = []
        for item in calendar or []:
            if not self._macro_relevant(item, base, is_crypto=is_crypto):
                continue
            title = str(item.get("event") or item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            country = str(item.get("country") or item.get("region") or "").strip()
            insight = item.get("ai_insight") if isinstance(item.get("ai_insight"), dict) else {}
            macro_events.append({
                "kind": "macro",
                "title": title[:300],
                "title_en": str(item.get("name_en") or item.get("event_en") or item.get("title_en") or "")[:300],
                "summary": self._calendar_summary(item),
                "summary_zh": str(insight.get("summary") or "")[:600],
                "summary_en": str(insight.get("summary_en") or "")[:600],
                "source": str(calendar_payload.get("source") or "economic_calendar"),
                "url": "",
                "published_at": str(item.get("date") or item.get("datetime") or item.get("time") or ""),
                "importance": str(item.get("importance") or item.get("impact") or ""),
                "country": country,
                "event_type": str(insight.get("event_type") or "macro"),
                "actual": item.get("actual"),
                "forecast": item.get("forecast"),
                "previous": item.get("previous"),
            })
            if len(macro_events) >= 2:
                break
        events = direct_events[:max_events]
        events.extend(macro_events[:max(0, max_events - len(events))])
        source_status["calendar"] = {
            "provider": calendar_payload.get("source") if isinstance(calendar_payload, dict) else "",
            "status": calendar_payload.get("status") if isinstance(calendar_payload, dict) else "",
            "count": len(calendar or []),
            "relevant_count": len(macro_events),
        }
        return events[:max_events], source_status

    def _analyze_jev(self, symbol: str, market_type: str, events: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
        base_url = str(config["base_url"]).rstrip("/")
        url = base_url if base_url.endswith("/systemone") else f"{base_url}/systemone"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
            json={
                "model": config["model"],
                "state": {"symbol": symbol, "market_type": market_type, "events": events},
                "questions": JEV_QUESTIONS,
            },
            timeout=config["timeout_seconds"],
        )
        response.raise_for_status()
        body = response.json()
        answers = body.get("answers") or body.get("result") or body.get("data") or {}
        resolved: dict[str, tuple[str, float]] = {}
        for name, options in JEV_OPTIONS.items():
            answer = self._jev_answer(answers, name)
            choice = str(answer.get("choice") or answer.get("selected") or "").strip().lower()
            probabilities = answer.get("probabilities") or answer.get("probability") or {}
            if choice not in options or set(probabilities) != options:
                raise ValueError(f"invalid JEV {name} answer")
            numeric = {key: float(value) for key, value in probabilities.items()}
            if any(value < 0 or value > 1 for value in numeric.values()) or abs(sum(numeric.values()) - 1) > 0.001:
                raise ValueError(f"invalid JEV {name} probabilities")
            confidence = float(answer.get("confidence", numeric.get(choice, 0)))
            if choice != max(numeric, key=numeric.get) or confidence < config["min_confidence"]:
                raise ValueError(f"JEV confidence below threshold for {name}")
            resolved[name] = (choice, min(max(confidence, 0.0), 1.0))
        confidence = min(value[1] for value in resolved.values())
        return {
            "direction": resolved["direction"][0],
            "confidence": confidence,
            "impact": resolved["impact"][0],
            "relevance": resolved["relevance"][0],
            "freshness": resolved["freshness"][0],
            "summary": "",
            "provider": "jev",
            "model": config["model"],
        }

    def _analyze_llm(
        self,
        symbol: str,
        market_type: str,
        events: list[dict[str, Any]],
        llm: LLMService,
    ) -> dict[str, Any]:
        model = llm.get_default_model()
        content = llm.call_llm_api(
            [
                {
                    "role": "system",
                    "content": (
                        "You are an event-impact classifier. Use only the supplied evidence. Return strict JSON with "
                        "direction (bullish, bearish, neutral, or mixed), confidence (0 to 1), impact (high, medium, "
                        "or low), relevance (high, medium, or low), freshness (fresh, mixed, or stale), and summary. "
                        "The summary must be concise and state uncertainty. This output is reference-only and must "
                        "never approve, reject, or execute an order."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"symbol": symbol, "market_type": market_type, "events": events},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            model=model,
            temperature=0,
            use_fallback=True,
            use_json_mode=True,
            try_alternative_providers=True,
            timeout_seconds=20,
        )
        payload = self._json_object(content)
        direction = str(payload.get("direction") or "").lower()
        impact = str(payload.get("impact") or "").lower()
        relevance = str(payload.get("relevance") or "").lower()
        freshness = str(payload.get("freshness") or "").lower()
        if direction not in JEV_OPTIONS["direction"] or impact not in JEV_OPTIONS["impact"]:
            raise ValueError("invalid LLM event direction or impact")
        if relevance not in JEV_OPTIONS["relevance"] or freshness not in JEV_OPTIONS["freshness"]:
            raise ValueError("invalid LLM event relevance or freshness")
        confidence = min(max(float(payload.get("confidence") or 0), 0.0), 1.0)
        return {
            "direction": direction,
            "confidence": confidence,
            "impact": impact,
            "relevance": relevance,
            "freshness": freshness,
            "summary": str(payload.get("summary") or "")[:1000],
            "provider": "llm",
            "model": model,
        }

    @staticmethod
    def _settings() -> dict[str, Any]:
        values: dict[str, Any] = {}
        try:
            from app.services.settings.env_file import read_env_file
            values = read_env_file()
        except Exception as exc:
            logger.debug("Event Radar settings file refresh skipped: %s", exc)

        def value(key: str, default: Any) -> Any:
            return values.get(key, os.getenv(key, default))

        def numeric(key: str, default: float) -> float:
            try:
                return float(value(key, default))
            except (TypeError, ValueError):
                logger.warning("Invalid %s setting; using default %s", key, default)
                return default

        def boolean(key: str, default: bool) -> bool:
            raw_default = "true" if default else "false"
            return str(value(key, raw_default)).strip().lower() in {"1", "true", "yes", "on"}

        contact_email = str(value("BRAND_CONTACT_EMAIL", "support@quantdinger.com") or "").strip()
        sec_user_agent = str(value("SEC_EDGAR_USER_AGENT", "") or "").strip()
        if not sec_user_agent and contact_email:
            sec_user_agent = f"QuantDinger Event Radar {contact_email}"
        return {
            "enabled": boolean("EVENT_RADAR_ENABLED", True),
            "jev_min_confidence": min(max(numeric("EVENT_RADAR_JEV_MIN_CONFIDENCE", 0.65), 0.0), 1.0),
            "news_lookback_days": min(max(int(numeric("EVENT_RADAR_NEWS_LOOKBACK_DAYS", 2)), 1), 30),
            "max_events": min(max(int(numeric("EVENT_RADAR_MAX_EVENTS", 8)), 1), 20),
            "crypto_rss_enabled": boolean("EVENT_RADAR_CRYPTO_RSS_ENABLED", True),
            "yahoo_finance_rss_enabled": boolean("EVENT_RADAR_YAHOO_FINANCE_RSS_ENABLED", True),
            "sec_edgar_enabled": boolean("EVENT_RADAR_SEC_EDGAR_ENABLED", True),
            "sec_edgar_user_agent": sec_user_agent,
        }

    @staticmethod
    def _jev_config(min_confidence: float) -> dict[str, Any]:
        values: dict[str, Any] = {}
        try:
            from app.services.settings.env_file import read_env_file
            values = read_env_file()
        except Exception:
            pass

        def value(key: str, default: str = "") -> str:
            return str(values.get(key, os.getenv(key, default)) or default).strip()

        return {
            "api_key": value("JEV_API_KEY"),
            "base_url": value("JEV_BASE_URL", "https://api.typesafe.ai/v1"),
            "model": value("JEV_MODEL", "jev-latest"),
            "timeout_seconds": min(max(float(value("JEV_TIMEOUT_SECONDS", "8")), 1.0), 30.0),
            "min_confidence": min_confidence,
        }

    def _persist(self, user_id: int, payload: dict[str, Any]) -> None:
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    """
                    INSERT INTO qd_event_radar_analyses
                    (analysis_uid, user_id, symbol, market_type, direction, confidence, impact,
                     relevance, freshness, summary, provider, model, fallback_reason,
                     source_status_json, events_json, billing_json, latency_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?::jsonb, ?, ?)
                    """,
                    (
                        payload["analysis_id"], user_id, payload["symbol"], payload["market_type"],
                        payload["direction"], payload["confidence"], payload["impact"], payload["relevance"],
                        payload["freshness"], payload["summary"], payload["provider"], payload["model"],
                        payload["fallback_reason"], json.dumps(payload["source_status"], ensure_ascii=False),
                        json.dumps(payload["events"], ensure_ascii=False), json.dumps(payload["billing"]),
                        payload["latency_ms"], payload["created_at"],
                    ),
                )
                db.commit()
                cur.close()
        except Exception as exc:
            logger.warning("Event Radar audit persistence failed: %s", exc)

    def _latest(self, user_id: int, symbol: str, market_type: str) -> dict[str, Any] | None:
        normalized_symbol = self._normalize_symbol(symbol)
        if not normalized_symbol:
            return None
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    """
                    SELECT analysis_uid, symbol, market_type, direction, confidence, impact, relevance,
                           freshness, summary, provider, model, fallback_reason, source_status_json,
                           events_json, billing_json, latency_ms, created_at
                    FROM qd_event_radar_analyses
                    WHERE user_id = ? AND symbol = ?
                      AND (? = '' OR LOWER(market_type) = LOWER(?))
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (user_id, normalized_symbol, str(market_type or ""), str(market_type or "")),
                )
                row = cur.fetchone()
                cur.close()
            if not row:
                return None
            source_status = self._json_value(row.get("source_status_json"), {})
            events = self._json_value(row.get("events_json"), [])
            for event in events:
                if not isinstance(event, dict) or event.get("kind") != "macro":
                    continue
                event.setdefault("event_type", self._legacy_macro_type(event.get("title")))
                title = str(event.get("title") or "").strip()
                if title and not re.search(r"[\u3400-\u9fff]", title):
                    event.setdefault("title_en", title)
            return {
                "analysis_id": row.get("analysis_uid"),
                "symbol": row.get("symbol"),
                "market_type": row.get("market_type"),
                "direction": row.get("direction"),
                "confidence": float(row.get("confidence") or 0),
                "impact": row.get("impact"),
                "relevance": row.get("relevance"),
                "freshness": row.get("freshness"),
                "summary": row.get("summary") or "",
                "provider": row.get("provider") or "",
                "model": row.get("model") or "",
                "fallback_reason": row.get("fallback_reason") or "",
                "source_status": source_status,
                "events": events,
                "billing": self._json_value(row.get("billing_json"), {}),
                "latency_ms": int(row.get("latency_ms") or 0),
                "created_at": str(row.get("created_at") or ""),
                "reference_only": True,
            }
        except Exception as exc:
            logger.debug("Event Radar latest lookup skipped: %s", exc)
            return None

    @staticmethod
    def _refund(user_id: int, receipt: dict[str, Any]) -> dict[str, Any]:
        charged = int(receipt.get("charged") or 0)
        if charged <= 0:
            return receipt
        ok, message = get_billing_service().add_credits(
            user_id=user_id,
            amount=charged,
            action="refund",
            remark="event_radar_provider_unavailable",
            reference_id=str(receipt.get("reference_id") or ""),
        )
        return {
            **receipt,
            "refunded": charged if ok else 0,
            "status": "refunded" if ok else "refund_failed",
            "refund_message": str(message or ""),
        }

    @staticmethod
    def _calendar_summary(item: dict[str, Any]) -> str:
        parts = []
        for key in ("actual", "forecast", "previous"):
            value = item.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        return ", ".join(parts)

    @staticmethod
    def _append_unique(events: list[dict[str, Any]], item: dict[str, Any], seen: set[str]) -> None:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        key = url or title.lower()
        if not title or not key or key in seen:
            return
        seen.add(key)
        events.append(item)

    @staticmethod
    def _news_relevant(item: dict[str, Any], base: str, asset_name: str, *, is_crypto: bool) -> bool:
        text = " ".join((str(item.get("title") or ""), str(item.get("summary") or ""))).lower()
        if not text.strip():
            return False
        aliases = {str(base or "").strip().lower(), str(asset_name or "").strip().lower()}
        if not is_crypto:
            short_name = re.sub(
                r"\s+(?:incorporated|inc\.?|corporation|corp\.?|company|co\.?|limited|ltd\.?|plc|holdings?|group)$",
                "",
                str(asset_name or "").strip(),
                flags=re.IGNORECASE,
            ).strip().lower()
            if len(short_name) >= 3:
                aliases.add(short_name)
        if is_crypto:
            crypto_aliases = {
                "BTC": {"bitcoin"}, "ETH": {"ethereum", "ether"}, "SOL": {"solana"},
                "BNB": {"binance coin"}, "XRP": {"ripple"}, "DOGE": {"dogecoin"},
            }
            aliases.update(crypto_aliases.get(base, set()))
        for alias in aliases:
            if alias and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _macro_relevant(item: dict[str, Any], base: str, *, is_crypto: bool) -> bool:
        insight = item.get("ai_insight") if isinstance(item.get("ai_insight"), dict) else {}
        event_type = str(insight.get("event_type") or "").lower()
        importance = str(item.get("importance") or item.get("impact") or "").lower()
        core_types = {"inflation", "central_bank", "jobs", "growth"}
        if event_type not in core_types:
            return False
        affected = insight.get("affected_assets") if isinstance(insight, dict) else []
        affected_symbols = {
            str(asset.get("symbol") or "").upper()
            for asset in (affected or []) if isinstance(asset, dict)
        }
        if is_crypto:
            return base in affected_symbols and importance in {"high", "medium"}
        return importance == "high"

    @staticmethod
    def _legacy_macro_type(title: Any) -> str:
        value = str(title or "").lower()
        rules = (
            (("cpi", "ppi", "pce", "inflation", "通胀", "物价"), "inflation"),
            (("fomc", "fed", "rate decision", "interest rate", "联储", "央行", "利率"), "central_bank"),
            (("nonfarm", "payroll", "jobless", "unemployment", "非农", "就业", "失业", "初请"), "jobs"),
            (("gdp", "pmi", "ism", "retail sales", "industrial production", "零售", "制造业", "工业产出"), "growth"),
        )
        for keywords, event_type in rules:
            if any(keyword in value for keyword in keywords):
                return event_type
        return "macro"

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol or "").strip().upper().replace("_", "/")[:80]

    @staticmethod
    def _jev_answer(answers: Any, key: str) -> dict[str, Any]:
        if not isinstance(answers, dict):
            return {}
        value = answers.get(key)
        if isinstance(value, dict):
            return value
        nested = answers.get("answers")
        return nested.get(key, {}) if isinstance(nested, dict) and isinstance(nested.get(key), dict) else {}

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        text = str(value or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:].lstrip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("Event Radar response must be a JSON object")
        return parsed

    @staticmethod
    def _json_value(value: Any, default: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value) if value else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return " ".join(str(exc or exc.__class__.__name__).split())[:300]


_service: EventRadarService | None = None


def get_event_radar_service() -> EventRadarService:
    global _service
    if _service is None:
        _service = EventRadarService()
    return _service
