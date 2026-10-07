"""Process-local sharing for public exchange price streams."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping

from app.services.market_price_stream import PriceFeedSnapshot, PublicMarketPriceFeed


RestFallback = Callable[[], Dict[str, float]]


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() not in {"0", "false", "no", "off"}


def _normalized_market_type(value: object) -> str:
    market_type = str(value or "spot").strip().lower()
    return "swap" if market_type in {"future", "futures", "perp", "perpetual"} else market_type


def _instrument_identity(instrument: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(instrument.get("api_family") or "").strip().lower(),
        str(instrument.get("instrument_id") or "").strip().upper(),
        str(instrument.get("symbol") or "").strip().upper(),
        str(instrument.get("key") or "").strip(),
    )


def public_feed_key(
    *,
    exchange_id: str,
    market_type: str,
    instruments: Iterable[Mapping[str, Any]],
) -> tuple[str, str, tuple[tuple[str, str, str, str], ...]]:
    """Return an exchange-scoped identity for an exact public subscription set."""
    return (
        str(exchange_id or "").strip().lower(),
        _normalized_market_type(market_type),
        tuple(sorted(_instrument_identity(item) for item in instruments)),
    )


@dataclass
class _SharedFeedEntry:
    feed: PublicMarketPriceFeed
    references: int = 0
    fallback_lock: threading.Lock = field(default_factory=threading.Lock)
    fallback_prices: Dict[str, float] = field(default_factory=dict)
    fallback_at: float = 0.0

    def load_fallback(self, loader: RestFallback, ttl_seconds: float) -> Dict[str, float]:
        now = time.monotonic()
        with self.fallback_lock:
            if self.fallback_at > 0 and now - self.fallback_at <= ttl_seconds:
                return dict(self.fallback_prices)
            values = {
                str(key): float(value or 0.0)
                for key, value in (loader() or {}).items()
                if float(value or 0.0) > 0
            }
            self.fallback_prices = values
            self.fallback_at = time.monotonic()
            return dict(values)


class PublicMarketPriceFeedHandle:
    """Reference-counted access to a shared or exclusive public price feed."""

    def __init__(
        self,
        *,
        entry: _SharedFeedEntry,
        rest_fallback: RestFallback,
        release: Callable[[], None],
        fallback_ttl_seconds: float,
    ) -> None:
        self._entry = entry
        self._rest_fallback = rest_fallback
        self._release = release
        self._fallback_ttl_seconds = max(0.05, float(fallback_ttl_seconds or 0.0))
        self._released = False
        self._lock = threading.Lock()

    def snapshot(self, *, max_age_seconds: float = 10.0) -> PriceFeedSnapshot:
        return self._entry.feed.snapshot(
            max_age_seconds=max_age_seconds,
            rest_fallback=lambda: self._entry.load_fallback(
                self._rest_fallback,
                self._fallback_ttl_seconds,
            ),
        )

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()

    def stop(self, timeout: float = 3.0) -> None:
        del timeout
        self.release()


class SharedPublicMarketPriceFeedRegistry:
    """Own one public feed per exchange, market type, and exact instrument set."""

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str, tuple[tuple[str, str, str, str], ...]],
            _SharedFeedEntry,
        ] = {}
        self._lock = threading.Lock()

    def acquire(
        self,
        *,
        exchange_id: str,
        market_type: str,
        instruments: Iterable[Mapping[str, Any]],
        rest_fallback: RestFallback,
        fallback_ttl_seconds: float = 1.0,
    ) -> PublicMarketPriceFeedHandle:
        instrument_list = [dict(item) for item in instruments]
        key = public_feed_key(
            exchange_id=exchange_id,
            market_type=market_type,
            instruments=instrument_list,
        )
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                feed = PublicMarketPriceFeed(
                    exchange_id=key[0],
                    market_type=key[1],
                    instruments=instrument_list,
                    rest_fallback=lambda: {},
                )
                entry = _SharedFeedEntry(feed=feed, references=0)
                self._entries[key] = entry
                feed.start()
            entry.references += 1

        return PublicMarketPriceFeedHandle(
            entry=entry,
            rest_fallback=rest_fallback,
            fallback_ttl_seconds=fallback_ttl_seconds,
            release=lambda: self._release(key, entry),
        )

    def _release(
        self,
        key: tuple[str, str, tuple[tuple[str, str, str, str], ...]],
        entry: _SharedFeedEntry,
    ) -> None:
        stop_feed = False
        with self._lock:
            current = self._entries.get(key)
            if current is not entry:
                return
            entry.references = max(0, entry.references - 1)
            if entry.references == 0:
                self._entries.pop(key, None)
                stop_feed = True
        if stop_feed:
            entry.feed.stop()

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "feeds": len(self._entries),
                "references": sum(entry.references for entry in self._entries.values()),
            }

    def reset(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.feed.stop()


shared_public_market_price_feed_registry = SharedPublicMarketPriceFeedRegistry()


def acquire_public_market_price_feed(
    *,
    exchange_id: str,
    market_type: str,
    instruments: Iterable[Mapping[str, Any]],
    rest_fallback: RestFallback,
) -> PublicMarketPriceFeedHandle:
    instrument_list = [dict(item) for item in instruments]
    fallback_ttl_seconds = max(
        0.05,
        float(os.getenv("SHARED_PUBLIC_MARKET_FALLBACK_TTL_SEC", "1")),
    )
    if _enabled(os.getenv("SHARED_PUBLIC_MARKET_FEED_ENABLED", "1")):
        return shared_public_market_price_feed_registry.acquire(
            exchange_id=exchange_id,
            market_type=market_type,
            instruments=instrument_list,
            rest_fallback=rest_fallback,
            fallback_ttl_seconds=fallback_ttl_seconds,
        )

    feed = PublicMarketPriceFeed(
        exchange_id=exchange_id,
        market_type=market_type,
        instruments=instrument_list,
        rest_fallback=lambda: {},
    )
    entry = _SharedFeedEntry(feed=feed, references=1)
    feed.start()
    return PublicMarketPriceFeedHandle(
        entry=entry,
        rest_fallback=rest_fallback,
        fallback_ttl_seconds=fallback_ttl_seconds,
        release=feed.stop,
    )


__all__ = [
    "PublicMarketPriceFeedHandle",
    "SharedPublicMarketPriceFeedRegistry",
    "acquire_public_market_price_feed",
    "public_feed_key",
    "shared_public_market_price_feed_registry",
]
