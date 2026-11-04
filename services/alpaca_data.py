"""Market data from Alpaca's free plan (real-time IEX feed) via the official `alpaca-py` SDK.

Provides the same interface as `daytrader.MassiveData`:
    intraday(symbol, minutes, now) -> Bars
    daily_closes(symbol, now) -> pd.Series
    screener(source) -> [{"symbol", "price", "volume", "change_pct"}]
plus batched versions (`intraday_many`, `daily_many`) and `quote_ask`, so one loop needs only a
few requests (the free plan allows 200/min).

Orders still go through SnapTrade; these keys are used for market data only.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from django.conf import settings

from services.errors import MarketDataError

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
SESSION_OPEN, SESSION_CLOSE = dtime(9, 30), dtime(16, 0)
CHUNK = 100  # symbols per batched request


class AlpacaDataError(MarketDataError):
    """Alpaca market data could not be fetched."""


def _in_session(ts: datetime) -> bool:
    local = ts.astimezone(NEW_YORK)
    return local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE


class AlpacaData:
    def __init__(self, bars_client=None, screener_client=None, feed: str | None = None):
        self.feed = (feed or getattr(settings, "ALPACA_DATA_FEED", "iex") or "iex").lower()
        if bars_client is None or screener_client is None:
            key, secret = settings.ALPACA_API_KEY_ID, settings.ALPACA_API_SECRET_KEY
            if not key or not secret:
                raise AlpacaDataError("ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set in .env.")
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.historical.screener import ScreenerClient

            bars_client = bars_client or StockHistoricalDataClient(api_key=key, secret_key=secret)
            screener_client = screener_client or ScreenerClient(api_key=key, secret_key=secret)
        self.bars_client = bars_client
        self.screener_client = screener_client
        self._daily_cache: dict[str, pd.Series] = {}

    # -- helpers ----------------------------------------------------------------------
    def _feed(self):
        from alpaca.data.enums import DataFeed

        return DataFeed(self.feed)

    def _call(self, description, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # alpaca.common.exceptions.APIError, network errors
            status = getattr(exc, "status_code", None)
            hint = ""
            if status in (401, 403):
                hint = " (check ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, and that your plan includes this data)"
            elif status == 429:
                hint = " (rate limit: free plan allows 200 requests/min)"
            raise AlpacaDataError(f"Alpaca {description} failed{f' [{status}]' if status else ''}: {exc}{hint}") from exc

    def _bars(self, symbols: list[str], timeframe, start: datetime, end: datetime | None = None) -> dict:
        from alpaca.data.requests import StockBarsRequest

        out: dict[str, list] = {}
        symbols = sorted({s.upper() for s in symbols})
        for i in range(0, len(symbols), CHUNK):
            req = StockBarsRequest(
                symbol_or_symbols=symbols[i : i + CHUNK], timeframe=timeframe, start=start, end=end, feed=self._feed()
            )
            barset = self._call("bars", self.bars_client.get_stock_bars, req)
            for sym, bars in (getattr(barset, "data", None) or {}).items():
                out[sym.upper()] = list(bars)
        return out

    # -- intraday ---------------------------------------------------------------------
    def intraday_many(self, symbols: list[str], minutes: int, now: datetime, days_back: int = 5) -> dict:
        """{SYMBOL: Bars} for many symbols in one (or a few) requests."""
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        from services.daytrader import Bars

        if not symbols:
            return {}
        raw = self._bars(symbols, TimeFrame(minutes, TimeFrameUnit.Minute), now - timedelta(days=days_back))
        cutoff = now - timedelta(minutes=minutes)
        result = {}
        for sym in {s.upper() for s in symbols}:
            bars = [b for b in raw.get(sym, []) if _in_session(b.timestamp)]
            closed = [b for b in bars if b.timestamp <= cutoff]
            series = pd.Series([float(b.close) for b in closed], index=[b.timestamp for b in closed], dtype=float)
            result[sym] = Bars(series, float(bars[-1].close) if bars else None)
        return result

    def intraday(self, symbol: str, minutes: int, now: datetime):
        return self.intraday_many([symbol], minutes, now)[symbol.upper()]

    # -- daily ------------------------------------------------------------------------
    def daily_many(self, symbols: list[str], now: datetime) -> dict:
        from alpaca.data.timeframe import TimeFrame

        today = now.astimezone(NEW_YORK).date()
        missing = [s.upper() for s in symbols if s.upper() not in self._daily_cache]
        if missing:
            raw = self._bars(missing, TimeFrame.Day, now - timedelta(days=120))
            for sym in missing:
                done = [b for b in raw.get(sym, []) if b.timestamp.astimezone(NEW_YORK).date() < today]
                self._daily_cache[sym] = pd.Series([float(b.close) for b in done], dtype=float)
        return {s.upper(): self._daily_cache[s.upper()] for s in symbols}

    def daily_closes(self, symbol: str, now: datetime) -> pd.Series:
        return self.daily_many([symbol], now)[symbol.upper()]

    # -- quotes & snapshots -------------------------------------------------------------
    def snapshots(self, symbols: list[str]) -> dict:
        from alpaca.data.requests import StockSnapshotRequest

        out = {}
        symbols = sorted({s.upper() for s in symbols})
        for i in range(0, len(symbols), CHUNK):
            req = StockSnapshotRequest(symbol_or_symbols=symbols[i : i + CHUNK], feed=self._feed())
            out.update({k.upper(): v for k, v in (self._call("snapshots", self.bars_client.get_stock_snapshot, req) or {}).items()})
        return out

    def quote_ask(self, symbol: str) -> float | None:
        from alpaca.data.requests import StockLatestQuoteRequest

        try:
            quotes = self._call(
                "quote", self.bars_client.get_stock_latest_quote,
                StockLatestQuoteRequest(symbol_or_symbols=symbol.upper(), feed=self._feed()),
            )
        except AlpacaDataError as exc:
            logger.warning("%s", exc)
            return None
        q = (quotes or {}).get(symbol.upper())
        ask = getattr(q, "ask_price", None)
        return float(ask) if ask else None

    # -- screener -------------------------------------------------------------------------
    def screener(self, source: str, watchlist: list[str] | None = None) -> list[dict]:
        """Candidate rows for the New Stock List.

        GAINERS: Alpaca top gainers; volume comes from Alpaca's most-active list (top 100), so
                 with a minimum volume set, only gainers that are also among the most active pass.
        MOST_ACTIVE: Alpaca most active by volume, with prices from snapshots.
        WATCHLIST: your tickers, priced from snapshots (volume filter not applied).
        """
        from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

        if source == "WATCHLIST":
            return self._rows_from_snapshots(watchlist or [], volumes=None)

        actives = self._call("most actives", self.screener_client.get_most_actives, MostActivesRequest(top=100))
        volumes = {a.symbol.upper(): int(a.volume or 0) for a in getattr(actives, "most_actives", []) or []}
        if source == "MOST_ACTIVE":
            return self._rows_from_snapshots(list(volumes), volumes=volumes)

        movers = self._call("market movers", self.screener_client.get_market_movers, MarketMoversRequest(top=50))
        return [
            {
                "symbol": m.symbol.upper(),
                "price": float(m.price or 0),
                "volume": volumes.get(m.symbol.upper(), 0),
                "change_pct": float(m.percent_change or 0),
            }
            for m in getattr(movers, "gainers", []) or []
        ]

    def _rows_from_snapshots(self, symbols: list[str], volumes: dict | None) -> list[dict]:
        rows = []
        for sym, snap in self.snapshots(symbols).items():
            trade, daily, prev = snap.latest_trade, snap.daily_bar, snap.previous_daily_bar
            price = float(getattr(trade, "price", None) or getattr(daily, "close", 0) or 0)
            prev_close = float(getattr(prev, "close", 0) or 0)
            rows.append(
                {
                    "symbol": sym,
                    "price": price,
                    "volume": volumes.get(sym, 0) if volumes is not None else None,  # watchlist: no filter
                    "change_pct": (price / prev_close - 1) * 100 if prev_close else 0.0,
                }
            )
        return rows

    # -- diagnostics ----------------------------------------------------------------------
    def test(self, symbol: str = "SPY", minutes: int = 5) -> list[tuple[bool, str]]:
        """Check what this account's plan can access. Returns [(ok, message)]."""
        from django.utils import timezone

        now = timezone.now()
        checks = []

        def run(label, fn):
            try:
                checks.append((True, f"{label}: {fn()}"))
            except Exception as exc:
                checks.append((False, f"{label}: {exc}"))

        run(f"{minutes}-min bars ({symbol}, {self.feed.upper()})", lambda: (
            lambda b: f"{len(b.closed)} closed bars, last price {b.last_price}")(self.intraday(symbol, minutes, now)))
        run(f"Daily bars ({symbol})", lambda: f"{len(self.daily_closes(symbol, now))} days")
        run(f"Latest quote ({symbol})", lambda: f"ask {self.quote_ask(symbol)}")
        run("Screener: top gainers", lambda: f"{len(self.screener('GAINERS'))} tickers")
        run("Screener: most active", lambda: f"{len(self.screener('MOST_ACTIVE'))} tickers")
        return checks
