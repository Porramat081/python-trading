"""Settrade Open API wrapper (Thai stocks on SET) built on the official `settrade_v2` SDK.

Thai market rules applied here:
  * Orders are sent in whole board lots of 100 shares (quantities are rounded down).
  * Orders are Limit orders at the last price +/- a slippage %, rounded to SET price steps.
  * broker_id "SANDBOX" uses Settrade's test environment (treated like paper trading).

Settrade does not publish response schemas, so parsers accept the expected field names plus
common alternatives. The Settings page's "Test connection" shows the raw keys returned.
"""
from __future__ import annotations

import logging
import math
import random
import time
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from django.conf import settings

from services.errors import BrokerError, OrderSkipped, TradingDisabledError

logger = logging.getLogger(__name__)

BOARD_LOT = 100
BANGKOK = ZoneInfo("Asia/Bangkok")

# SET equity price steps: (upper bound exclusive, tick size)
SET_TICKS = [
    (Decimal("2"), Decimal("0.01")),
    (Decimal("5"), Decimal("0.02")),
    (Decimal("10"), Decimal("0.05")),
    (Decimal("25"), Decimal("0.10")),
    (Decimal("100"), Decimal("0.25")),
    (Decimal("200"), Decimal("0.50")),
    (Decimal("400"), Decimal("1.00")),
    (None, Decimal("2.00")),
]


class SettradeServiceError(BrokerError):
    """Raised for Settrade configuration problems or API errors."""


# -- Thai market rules (pure functions) ---------------------------------------
def tick_size(price: Decimal) -> Decimal:
    for upper, tick in SET_TICKS:
        if upper is None or price < upper:
            return tick
    return SET_TICKS[-1][1]  # pragma: no cover


def round_to_tick(price: float | Decimal, direction: str) -> Decimal:
    """Round to a valid SET price. direction 'down' (buy limits) or 'up' (sell limits)."""
    price = Decimal(str(price))
    tick = tick_size(price)
    steps = (price / tick).to_integral_value(rounding=ROUND_FLOOR if direction == "down" else ROUND_CEILING)
    result = (steps * tick).quantize(tick)
    # Crossing a band boundary can change the tick; re-check once.
    if tick_size(result) != tick:
        tick = tick_size(result)
        steps = (result / tick).to_integral_value(rounding=ROUND_FLOOR if direction == "down" else ROUND_CEILING)
        result = (steps * tick).quantize(tick)
    return result


def to_board_lots(units: float | Decimal) -> int:
    """Round a share quantity down to whole board lots."""
    return int(math.floor(float(units) / BOARD_LOT)) * BOARD_LOT


def limit_price(last: float | Decimal, side: str, slippage_pct: float | Decimal = 0) -> Decimal:
    """Last price +slippage for BUY (rounded down to a tick) / -slippage for SELL (rounded up)."""
    last = Decimal(str(last))
    slip = Decimal(str(slippage_pct or 0)) / Decimal(100)
    if side == "BUY":
        return round_to_tick(last * (1 + slip), "down")
    return round_to_tick(last * (1 - slip), "up")


# -- response parsing helpers -------------------------------------------------
def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_datetime(value) -> datetime:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value)
    if text.isdigit():
        return _to_datetime(int(text))
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=BANGKOK)


def parse_candles(symbol: str, payload) -> list:
    """Accept a dict of parallel lists ({"time": [...], "open": [...], ...}) or a list of bars."""
    from services.massive_service import Candle

    symbol = symbol.upper()
    candles = []
    if isinstance(payload, dict) and isinstance(payload.get("time"), list):
        cols = payload
        for i, t in enumerate(cols["time"]):
            candles.append(
                Candle(
                    symbol=symbol,
                    timestamp=_to_datetime(t),
                    open=Decimal(str(cols["open"][i])),
                    high=Decimal(str(cols["high"][i])),
                    low=Decimal(str(cols["low"][i])),
                    close=Decimal(str(cols["close"][i])),
                    volume=int(float((cols.get("volume") or [0] * len(cols["time"]))[i] or 0)),
                )
            )
        return candles

    bars = payload if isinstance(payload, list) else _first(payload or {}, "candlesticks", "data", "items", default=[])
    for bar in bars or []:
        candles.append(
            Candle(
                symbol=symbol,
                timestamp=_to_datetime(_first(bar, "time", "timestamp", "date", "t")),
                open=Decimal(str(_first(bar, "open", "o"))),
                high=Decimal(str(_first(bar, "high", "h"))),
                low=Decimal(str(_first(bar, "low", "l"))),
                close=Decimal(str(_first(bar, "close", "c"))),
                volume=int(float(_first(bar, "volume", "v", default=0) or 0)),
            )
        )
    return candles


def parse_positions(payload) -> list[dict]:
    items = payload if isinstance(payload, list) else _first(
        payload or {}, "portfolioList", "portfolios", "data", default=[]
    )
    positions = []
    for p in items or []:
        units = _num(_first(p, "actualVolume", "currentVolume", "volume", "startVolume")) or 0.0
        available = _num(_first(p, "availableVolume", "sellableVolume"))
        price = _num(_first(p, "marketPrice", "lastPrice", "last"))
        avg = _num(_first(p, "averagePrice", "avgPrice", "costPrice"))
        value = _num(_first(p, "marketValue")) or (units * price if price is not None else None)
        cost = _num(_first(p, "amount", "costValue")) or (units * avg if avg is not None else None)
        if units <= 0:
            continue
        positions.append(
            {
                "symbol": str(_first(p, "symbol", "stockSymbol", default="?")).upper(),
                "description": "",
                "units": units,
                "available_units": available if available is not None else units,
                "price": price,
                "cost_basis": cost,
                "market_value": value,
                "currency": "THB",
            }
        )
    return positions


# -- service --------------------------------------------------------------------
class SettradeService:
    """Settrade operations for one BrokerageAccount (provider=SETTRADE)."""

    market = "TH"

    def __init__(self, account, investor_factory: Callable | None = None, max_retries: int = 3):
        self.account = account
        self.max_retries = max_retries
        self._investor_factory = investor_factory
        self._investor = None

    # -- connection ------------------------------------------------------------
    def _login(self):
        acc = self.account
        missing = [
            name for name, value in (
                ("broker ID", acc.settrade_broker_id), ("app ID", acc.settrade_app_id),
                ("app secret", acc.settrade_app_secret), ("app code", acc.settrade_app_code),
            ) if not value
        ]
        if missing:
            raise SettradeServiceError(f"Settrade account is missing: {', '.join(missing)}.")
        if self._investor_factory:
            return self._investor_factory(acc)

        from settrade_v2 import Investor
        from settrade_v2.config import config as sdk_config

        # The SDK keeps the environment in a module-level setting and only switches it *to*
        # sandbox ('uat'), never back. Set it explicitly before every login.
        sdk_config["environment"] = "uat" if acc.is_paper else "prod"
        return Investor(
            app_id=acc.settrade_app_id,
            app_secret=acc.settrade_app_secret,
            app_code=acc.settrade_app_code,
            broker_id=acc.settrade_broker_id.strip(),
            is_auto_queue=False,
        )

    @property
    def investor(self):
        if self._investor is None:
            self._investor = self._call("login", self._login, retry=True)
        return self._investor

    def _equity(self):
        return self.investor.Equity(account_no=self.account.account_id)

    def _call(self, description: str, func: Callable, *args, retry: bool = True, **kwargs) -> Any:
        """Run an SDK call, converting errors and retrying rate limits / server errors."""
        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except BrokerError:
                raise
            except Exception as exc:  # SettradeError, requests/network errors, ...
                status = getattr(exc, "status_code", None)
                # Rate limits are always safe to retry; network/server errors only when allowed
                # (never for orders, which could otherwise be placed twice).
                retryable = status == 429 or (retry and (status is None or status >= 500))
                if retryable and attempt < self.max_retries:
                    wait = backoff + random.uniform(0, 0.5)
                    logger.warning("Settrade %s failed (%s); retrying in %.1fs", description, exc, wait)
                    time.sleep(wait)
                    backoff *= 2
                    continue
                code = getattr(exc, "code", "")
                raise SettradeServiceError(
                    f"Settrade {description} failed{f' ({code})' if code else ''}: {exc}", status=status
                ) from exc
        raise SettradeServiceError(f"Settrade {description} failed.")  # pragma: no cover

    # -- market data -------------------------------------------------------------
    def get_daily_candles(self, symbol: str, start: date, end: date) -> list:
        days = max((end - start).days, 1)
        # ~250 trading days per year; ask for a little more and trim to the requested range.
        limit = min(int(days * 0.75) + 10, 1000)
        market = self.investor.MarketData()
        payload = self._call("candles", market.get_candlestick, symbol=symbol.upper(), interval="1d", limit=limit)
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=BANGKOK)
        return [c for c in parse_candles(symbol, payload) if c.timestamp >= start_dt]

    def get_last_price(self, symbol: str) -> float | None:
        market = self.investor.MarketData()
        quote = self._call("quote", market.get_quote_symbol, symbol=symbol.upper())
        return _num(_first(quote or {}, "last", "lastPrice", "close", "prior"))

    # -- account data ----------------------------------------------------------------
    def get_balances(self) -> dict:
        info = self._call("account info", self._equity().get_account_info) or {}
        cash = _num(_first(info, "cashBalance", "cash", "cashBalanceT0"))
        buying_power = _num(_first(info, "lineAvailable", "purchasingPower", "buyingPower", "ppTrade"))
        net = _num(_first(info, "equity", "totalEquity", "netAssetValue", "totalAssets"))
        if net is None:
            try:
                holdings = sum(p["market_value"] or 0 for p in self.get_positions())
            except BrokerError:
                holdings = 0
            net = (cash or 0) + holdings
        return {"net_value": net, "currency": "THB", "cash": cash or 0, "buying_power": buying_power, "raw": info}

    def get_positions(self) -> list[dict]:
        payload = self._call("portfolios", self._equity().get_portfolios)
        return parse_positions(payload)

    def get_position_units(self, symbol: str) -> float:
        """Shares that can be sold now (availableVolume when the broker reports it)."""
        symbol = symbol.upper()
        return sum(p["available_units"] for p in self.get_positions() if p["symbol"] == symbol)

    # -- trading -------------------------------------------------------------------------
    def execute_order(
        self, symbol: str, side: str, units, ref_price: float | None = None, slippage_pct=0
    ) -> dict:
        """Place a Thai limit order. Returns {"order_id", "status", "price", "quantity", "raw"}."""
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise SettradeServiceError(f"Unsupported order side: {side}")
        if not settings.TRADING_ENABLED:
            raise TradingDisabledError("TRADING_ENABLED is False; order not sent (dry run).")
        if not self.account.is_paper and not settings.LIVE_TRADING:
            raise TradingDisabledError(
                "This is a live Settrade account but LIVE_TRADING is False; order refused. "
                "Use broker ID SANDBOX for testing."
            )
        volume = to_board_lots(units)
        if volume <= 0:
            raise OrderSkipped(
                f"Quantity {float(units):g} is below one board lot ({BOARD_LOT} shares); order not sent."
            )
        if not self.account.settrade_pin:
            raise SettradeServiceError("Settrade trading PIN is not set (Settings page).")

        last = None
        try:
            last = self.get_last_price(symbol)
        except BrokerError as exc:
            logger.warning("Quote for %s failed, using last close: %s", symbol, exc)
        last = last or ref_price
        if not last:
            raise SettradeServiceError(f"No price available for {symbol}.")
        price = limit_price(last, side, slippage_pct)

        # Orders are not retried on server errors (could duplicate); only rate limits.
        result = self._call(
            "place order",
            self._equity().place_order,
            retry=False,
            pin=self.account.settrade_pin,
            side="Buy" if side == "BUY" else "Sell",
            symbol=symbol.upper(),
            volume=volume,
            price=float(price),
            price_type="Limit",
            validity_type="Day",
        ) or {}
        return {
            "order_id": str(_first(result, "orderNo", "order_no", "orderNumber", default="")),
            "status": str(_first(result, "status", "showStatus", default="submitted")),
            "price": float(price),
            "quantity": volume,
            "raw": result,
        }

    # -- diagnostics ---------------------------------------------------------------------
    def test_connection(self) -> dict:
        """Log in and read account info, holdings and one quote; report what came back."""
        report = {"ok": False, "steps": []}
        try:
            info = self._call("account info", self._equity().get_account_info) or {}
            report["steps"].append(f"Account info OK (fields: {', '.join(sorted(info)) or 'none'})")
            raw_positions = self._call("portfolios", self._equity().get_portfolios)
            keys = sorted(raw_positions) if isinstance(raw_positions, dict) else f"list[{len(raw_positions)}]"
            report["steps"].append(f"Portfolio OK ({len(parse_positions(raw_positions))} positions; keys: {keys})")
            candles = self.investor.MarketData().get_candlestick(symbol="PTT", interval="1d", limit=5)
            ckeys = sorted(candles) if isinstance(candles, dict) else type(candles).__name__
            report["steps"].append(f"Candles OK (PTT: {len(parse_candles('PTT', candles))} bars; keys: {ckeys})")
            report["ok"] = True
        except Exception as exc:
            report["steps"].append(f"Failed: {exc}")
        return report
