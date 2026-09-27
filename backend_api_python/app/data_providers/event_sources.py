"""Free, instrument-specific event sources used by Event Radar."""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
import re
from html import unescape
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests


COINDESK_RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
YAHOO_FINANCE_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SEC_RELEVANT_FORMS = {
    "8-K", "10-K", "10-Q", "20-F", "40-F", "6-K", "S-1", "S-3", "424B2", "424B3", "424B4",
    "SC 13D", "SC 13G", "DEF 14A", "4",
}

_ticker_cache: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})
_ticker_lock = threading.Lock()


def fetch_coindesk_events(*, days: int = 2, limit: int = 20, timeout: float = 8.0) -> list[dict[str, Any]]:
    response = requests.get(
        COINDESK_RSS_URL,
        headers={"User-Agent": "QuantDinger Event Radar/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    events: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _xml_text(item, "title")
        if not title:
            continue
        published = _xml_text(item, "pubDate")
        published_at = _rss_datetime(published)
        if published_at and published_at < cutoff:
            continue
        events.append({
            "kind": "news",
            "title": title[:300],
            "summary": _plain_text(_xml_text(item, "description"))[:600],
            "source": "CoinDesk RSS",
            "url": _xml_text(item, "link"),
            "published_at": published_at.isoformat() if published_at else published,
        })
        if len(events) >= max(1, limit):
            break
    return events


def fetch_yahoo_finance_events(
    ticker: str,
    *,
    days: int = 2,
    limit: int = 20,
    timeout: float = 8.0,
) -> list[dict[str, Any]]:
    normalized_ticker = str(ticker or "").strip().upper()
    if not normalized_ticker:
        return []
    response = requests.get(
        YAHOO_FINANCE_RSS_URL,
        params={"s": normalized_ticker, "region": "US", "lang": "en-US"},
        headers={"User-Agent": "QuantDinger Event Radar/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    events: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _xml_text(item, "title")
        if not title:
            continue
        published = _xml_text(item, "pubDate")
        published_at = _rss_datetime(published)
        if published_at and published_at < cutoff:
            continue
        events.append({
            "kind": "news",
            "title": title[:300],
            "summary": _plain_text(_xml_text(item, "description"))[:600],
            "source": "Yahoo Finance RSS",
            "url": _xml_text(item, "link"),
            "published_at": published_at.isoformat() if published_at else published,
        })
        if len(events) >= max(1, limit):
            break
    return events


def fetch_sec_filings(
    ticker: str,
    *,
    days: int = 2,
    limit: int = 10,
    user_agent: str,
    timeout: float = 8.0,
) -> list[dict[str, Any]]:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    company = _sec_ticker_map(headers, timeout).get(str(ticker or "").upper())
    if not company:
        return []
    cik = int(company["cik"])
    response = requests.get(SEC_SUBMISSIONS_URL.format(cik=cik), headers=headers, timeout=timeout)
    response.raise_for_status()
    recent = (response.json().get("filings") or {}).get("recent") or {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date()
    forms = recent.get("form") or []
    events: list[dict[str, Any]] = []
    for index, form in enumerate(forms):
        form = str(form or "").upper()
        if form not in SEC_RELEVANT_FORMS:
            continue
        filing_date = _array_value(recent, "filingDate", index)
        try:
            if datetime.fromisoformat(filing_date).date() < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        accession_raw = _array_value(recent, "accessionNumber", index)
        document = _array_value(recent, "primaryDocument", index)
        accession = accession_raw.replace("-", "")
        url = SEC_ARCHIVES_URL.format(cik=cik, accession=accession, document=document) if accession and document else ""
        description = _array_value(recent, "primaryDocDescription", index)
        events.append({
            "kind": "filing",
            "title": f"{ticker.upper()} · SEC EDGAR · {form}",
            "summary": description,
            "source": "SEC EDGAR",
            "url": url,
            "published_at": filing_date,
            "form": form,
        })
        if len(events) >= max(1, limit):
            break
    return events


def _sec_ticker_map(headers: dict[str, str], timeout: float) -> dict[str, dict[str, Any]]:
    global _ticker_cache
    cached_at, cached = _ticker_cache
    if cached and time.time() - cached_at < 12 * 60 * 60:
        return cached
    with _ticker_lock:
        cached_at, cached = _ticker_cache
        if cached and time.time() - cached_at < 12 * 60 * 60:
            return cached
        response = requests.get(SEC_TICKERS_URL, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        headers_row = payload.get("fields") or payload.get("headers") or ["cik", "name", "ticker", "exchange"]
        resolved: dict[str, dict[str, Any]] = {}
        for row in payload.get("data") or []:
            record = dict(zip(headers_row, row))
            ticker = str(record.get("ticker") or "").upper()
            cik = record.get("cik") or record.get("cik_str")
            if ticker and cik is not None:
                resolved[ticker] = {**record, "cik": int(cik)}
        _ticker_cache = (time.time(), resolved)
        return resolved


def _xml_text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return "" if node is None else " ".join(str(node.text or "").split())


def _plain_text(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _rss_datetime(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _array_value(payload: dict[str, Any], key: str, index: int) -> str:
    values = payload.get(key) or []
    return str(values[index] or "").strip() if index < len(values) else ""
