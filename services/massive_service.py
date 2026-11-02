"""Massive (formerly Polygon.io) market-data client and backfill helpers.

Uses the aggregates (bars) REST endpoint:
    GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class MassiveAPIError(Exception):
    """Raised when the Massive API returns an error or cannot be reached."""


@dataclass(frozen=True)
class Candle:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class MassiveClient:
    """Thin REST client with retry/backoff for rate limits (HTTP 429) and 5xx errors."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 5,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.MASSIVE_API_KEY
        self.base_url = (base_url or settings.MASSIVE_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        if not self.api_key:
            raise MassiveAPIError("MASSIVE_API_KEY is not configured.")

    # -- low level ----------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        backoff = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise MassiveAPIError(f"Network error calling Massive API: {exc}") from exc
                logger.warning("Massive network error (%s), retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise MassiveAPIError(
                        f"Massive API returned {response.status_code} after {attempt} attempts."
                    )
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                logger.warning(
                    "Massive API %s (rate limited/server error), retrying in %.0fs",
                    response.status_code,
                    wait,
                )
                time.sleep(wait)
                backoff *= 2
                continue

            if response.status_code >= 400:
                raise MassiveAPIError(
                    f"Massive API error {response.status_code}: {response.text[:300]}"
                )

            payload = response.json()
            if payload.get("status") == "ERROR":
                raise MassiveAPIError(payload.get("error") or payload.get("message") or "Unknown error")
            return payload
        raise MassiveAPIError("Massive API request failed.")  # pragma: no cover

    # -- public API ---------------------------------------------------------
    def get_aggregates(
        self,
        symbol: str,
        start_date: date | str,
        end_date: date | str,
        multiplier: int = 1,
        timespan: str = "day",
        adjusted: bool = True,
    ) -> list[Candle]:
        """Return OHLCV candles between two dates (inclusive), following pagination."""
        symbol = symbol.upper()
        url = (
            f"{self.base_url}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/"
            f"{_iso(start_date)}/{_iso(end_date)}"
        )
        params: dict | None = {"adjusted": str(adjusted).lower(), "sort": "asc", "limit": 50000}
        candles: list[Candle] = []
        while url:
            payload = self._get(url, params)
            for bar in payload.get("results") or []:
                candles.append(
                    Candle(
                        symbol=symbol,
                        timestamp=datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc),
                        open=Decimal(str(bar["o"])),
                        high=Decimal(str(bar["h"])),
                        low=Decimal(str(bar["l"])),
                        close=Decimal(str(bar["c"])),
                        volume=int(bar.get("v") or 0),
                    )
                )
            # next_url already carries the cursor and query params (minus apiKey).
            url = payload.get("next_url")
            params = None
        return candles


def _iso(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def fetch_historical_data(
    symbol: str,
    start_date: date | str,
    end_date: date | str,
    timespan: str = "day",
    multiplier: int = 1,
    client: MassiveClient | None = None,
) -> dict:
    """Backfill candles for `symbol` into StockData, skipping rows that already exist
    (the most recent existing candle is refreshed, in case it was a partial bar).

    Returns a summary dict: {"symbol", "fetched", "created", "skipped"}.
    """
    from trading.models import StockData

    client = client or MassiveClient()
    candles = client.get_aggregates(
        symbol, start_date, end_date, multiplier=multiplier, timespan=timespan
    )
    symbol = symbol.upper()
    if not candles:
        return {"symbol": symbol, "fetched": 0, "created": 0, "skipped": 0}

    existing = set(
        StockData.objects.filter(
            symbol=symbol,
            timestamp__gte=candles[0].timestamp,
            timestamp__lte=candles[-1].timestamp,
        ).values_list("timestamp", flat=True)
    )
    rows = [
        StockData(
            symbol=c.symbol,
            timestamp=c.timestamp,
            open_price=c.open,
            high_price=c.high,
            low_price=c.low,
            close_price=c.close,
            volume=c.volume,
        )
        for c in candles
    ]
    new_rows = [r for r in rows if r.timestamp not in existing]
    StockData.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)
    # Refresh the most recent stored candle: it may have been saved mid-session (partial bar).
    latest = [r for r in rows if r.timestamp in existing][-1:]
    for r in latest:
        StockData.objects.filter(symbol=symbol, timestamp=r.timestamp).update(
            open_price=r.open_price, high_price=r.high_price, low_price=r.low_price,
            close_price=r.close_price, volume=r.volume,
        )
    summary = {
        "symbol": symbol,
        "fetched": len(candles),
        "created": len(new_rows),
        "skipped": len(candles) - len(new_rows),
    }
    logger.info("Backfill %s: %s", symbol, summary)
    return summary
