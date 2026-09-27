from datetime import datetime, timezone
from email.utils import format_datetime

from app.data_providers import event_sources as module


class FakeResponse:
    def __init__(self, *, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_coindesk_feed_is_normalized_and_html_is_removed(monkeypatch):
    published = format_datetime(datetime.now(timezone.utc))
    xml = f"""<rss><channel><item>
      <title>Bitcoin ETF update</title>
      <description><![CDATA[<p>Fresh <strong>BTC</strong> flows.</p>]]></description>
      <link>https://example.test/bitcoin</link>
      <pubDate>{published}</pubDate>
    </item></channel></rss>""".encode()
    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: FakeResponse(content=xml))

    events = module.fetch_coindesk_events(days=2)

    assert len(events) == 1
    assert events[0]["title"] == "Bitcoin ETF update"
    assert events[0]["summary"] == "Fresh BTC flows."


def test_yahoo_finance_feed_is_ticker_scoped_and_normalized(monkeypatch):
    published = format_datetime(datetime.now(timezone.utc))
    xml = f"""<rss><channel><item>
      <title>Microsoft announces a new cloud product</title>
      <description><![CDATA[<p>Company-specific update.</p>]]></description>
      <link>https://finance.yahoo.com/news/microsoft-cloud</link>
      <pubDate>{published}</pubDate>
    </item></channel></rss>""".encode()
    captured = {}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse(content=xml)

    monkeypatch.setattr(module.requests, "get", fake_get)
    events = module.fetch_yahoo_finance_events("MSFT", days=2)

    assert captured["url"] == module.YAHOO_FINANCE_RSS_URL
    assert captured["params"]["s"] == "MSFT"
    assert events[0]["title"] == "Microsoft announces a new cloud product"
    assert events[0]["summary"] == "Company-specific update."
    assert events[0]["source"] == "Yahoo Finance RSS"


def test_sec_edgar_resolves_ticker_and_material_filings(monkeypatch):
    module._ticker_cache = (0.0, {})
    ticker_payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
    }
    submissions_payload = {
        "filings": {"recent": {
            "form": ["8-K", "4", "UPLOAD"],
            "filingDate": [datetime.now(timezone.utc).date().isoformat()] * 3,
            "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002", "0000320193-26-000003"],
            "primaryDocument": ["aapl-8k.htm", "xslF345X05/form4.xml", "letter.htm"],
            "primaryDocDescription": ["Current report", "Insider transaction", "Correspondence"],
        }},
    }

    def fake_get(url, **kwargs):
        if url == module.SEC_TICKERS_URL:
            return FakeResponse(payload=ticker_payload)
        return FakeResponse(payload=submissions_payload)

    monkeypatch.setattr(module.requests, "get", fake_get)
    events = module.fetch_sec_filings(
        "AAPL", days=2, user_agent="QuantDinger tests test@example.com",
    )

    assert [event["form"] for event in events] == ["8-K", "4"]
    assert all(event["source"] == "SEC EDGAR" for event in events)
    assert events[0]["url"].endswith("/000032019326000001/aapl-8k.htm")
