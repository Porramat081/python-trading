"""Momentum strategy engine: moving-average crossover on StockData candles.

Signal rules (evaluated on the latest closed candle):
  * BUY  - short MA crosses above long MA (was <= on the previous bar, now >)
  * SELL - short MA crosses below long MA (was >= on the previous bar, now <)
  * HOLD - otherwise
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
from django.utils import timezone

logger = logging.getLogger(__name__)

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"


@dataclass
class StrategyResult:
    strategy: str
    symbol: str
    signal: str
    bar_time: datetime | None = None
    close: float | None = None
    short_ma: float | None = None
    long_ma: float | None = None
    trade_log_id: int | None = None
    trade_status: str | None = None
    message: str = ""
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        parts = [f"[{self.strategy}] {self.symbol}: {self.signal}"]
        if self.close is not None:
            parts.append(f"close={self.close:.2f}")
        if self.short_ma is not None and self.long_ma is not None:
            parts.append(f"shortMA={self.short_ma:.2f} longMA={self.long_ma:.2f}")
        if self.extra.get("trend"):
            parts.append(f"trend={self.extra['trend']}")
        if self.trade_status:
            parts.append(f"trade={self.trade_status}")
        if self.message:
            parts.append(f"- {self.message}")
        return " ".join(parts)


# -- indicator maths (pure functions, easy to unit test) -------------------
def moving_average(closes: pd.Series, window: int, ma_type: str = "SMA") -> pd.Series:
    if ma_type == "EMA":
        return closes.ewm(span=window, adjust=False, min_periods=window).mean()
    return closes.rolling(window=window, min_periods=window).mean()


def compute_indicators(
    closes: pd.Series, short_window: int, long_window: int, ma_type: str = "SMA"
) -> pd.DataFrame:
    df = pd.DataFrame({"close": closes.astype(float)})
    df["short_ma"] = moving_average(df["close"], short_window, ma_type)
    df["long_ma"] = moving_average(df["close"], long_window, ma_type)
    diff = df["short_ma"] - df["long_ma"]
    prev = diff.shift(1)
    df["signal"] = HOLD
    df.loc[(prev <= 0) & (diff > 0), "signal"] = BUY
    df.loc[(prev >= 0) & (diff < 0), "signal"] = SELL
    return df


def generate_signal(closes: pd.Series, short_window: int, long_window: int, ma_type: str = "SMA") -> str:
    """Return BUY / SELL / HOLD for the most recent bar."""
    if len(closes) < long_window + 1:
        return HOLD
    df = compute_indicators(closes, short_window, long_window, ma_type)
    return str(df["signal"].iloc[-1])


def load_closes(symbol: str, limit: int | None = None) -> pd.Series:
    """Load close prices from StockData as a time-indexed Series (ascending)."""
    from trading.models import StockData

    qs = StockData.objects.filter(symbol=symbol.upper()).order_by("-timestamp")
    if limit:
        qs = qs[:limit]
    rows = list(qs.values_list("timestamp", "close_price"))
    rows.reverse()
    if not rows:
        return pd.Series(dtype=float)
    index, values = zip(*rows)
    return pd.Series([float(v) for v in values], index=pd.DatetimeIndex(index), name="close")


def chart_data(strategy, limit: int = 300) -> dict:
    """Prices + indicator lines + crossover markers for the dashboard chart."""
    closes = load_closes(strategy.symbol, limit=limit + strategy.long_window)
    if closes.empty:
        return {"labels": [], "close": [], "short_ma": [], "long_ma": [], "signals": []}
    df = compute_indicators(closes, strategy.short_window, strategy.long_window, strategy.ma_type)
    df = df.tail(limit)

    def clean(series):
        return [None if pd.isna(v) else round(float(v), 4) for v in series]

    labels = [ts.isoformat() for ts in df.index]
    signals = [
        {"x": labels[i], "y": float(row.close), "signal": row.signal}
        for i, row in enumerate(df.itertuples())
        if row.signal != HOLD
    ]
    return {
        "labels": labels,
        "close": clean(df["close"]),
        "short_ma": clean(df["short_ma"]),
        "long_ma": clean(df["long_ma"]),
        "signals": signals,
    }


# -- orchestration -----------------------------------------------------------
UP, DOWN = "UP", "DOWN"


class MomentumStrategy:
    """Evaluates a StrategyConfig and (optionally) sends orders via SnapTrade."""

    def __init__(self, config, snaptrade_service=None):
        self.config = config
        self._service = snaptrade_service

    @property
    def service(self):
        if self._service is None:
            from services.snaptrade_service import SnapTradeService

            self._service = SnapTradeService()
        return self._service

    def evaluate(self) -> StrategyResult:
        cfg = self.config
        closes = load_closes(cfg.symbol, limit=max(cfg.long_window * 5, 500))
        result = StrategyResult(strategy=cfg.name, symbol=cfg.symbol, signal=HOLD)
        if len(closes) < cfg.long_window + 1:
            result.message = (
                f"Not enough data: {len(closes)} candles, need {cfg.long_window + 1}. "
                "Run a backfill first."
            )
            return result

        df = compute_indicators(closes, cfg.short_window, cfg.long_window, cfg.ma_type)
        last = df.iloc[-1]
        result.signal = str(last["signal"])
        result.bar_time = df.index[-1].to_pydatetime()
        result.close = float(last["close"])
        result.short_ma = float(last["short_ma"])
        result.long_ma = float(last["long_ma"])
        result.extra["trend"] = UP if result.short_ma > result.long_ma else DOWN
        return result

    def _record(self, result: StrategyResult) -> None:
        cfg = self.config
        cfg.last_run = timezone.now()
        cfg.last_signal = result.signal
        cfg.trend = result.extra.get("trend", "")
        cfg.save(update_fields=["last_run", "last_signal", "trend", "last_signal_bar", "is_active"])
        logger.info("%s", result)

    def run(self, execute: bool = True) -> StrategyResult:
        """Evaluate the strategy, record the run and trade on a fresh crossover."""
        cfg = self.config
        result = self.evaluate()

        already_handled = (
            result.bar_time is not None
            and cfg.last_signal_bar is not None
            and cfg.last_signal_bar >= result.bar_time
        )
        if result.signal in (BUY, SELL) and already_handled:
            result.message = "Signal for this candle was already handled."
        elif result.signal in (BUY, SELL) and execute:
            self._trade(result, result.signal)
            cfg.last_signal_bar = result.bar_time
        elif result.signal in (BUY, SELL):
            result.message = "Evaluation only (no execution requested)."

        self._record(result)
        return result

    def start(self, backfill_days: int | None = None) -> StrategyResult:
        """Automatic setup for a newly created strategy.

        1. Backfill price history for the symbol.
        2. Evaluate the signal and current trend.
        3. Switch the strategy ON so the scheduler keeps running it.
        4. Trend-based entry: if the latest signal is BUY, or the short MA is above the
           long MA (uptrend) and the signal is not SELL, buy now. Otherwise wait for the
           next BUY crossover.
        """
        from services.massive_service import MassiveAPIError, fetch_historical_data

        cfg = self.config
        days = backfill_days or max(730, cfg.long_window * 4)
        end = timezone.now().date()
        backfill_note = ""
        try:
            summary = fetch_historical_data(cfg.symbol, end - timedelta(days=days), end)
            backfill_note = f"Backfilled {summary['created']} new candles. "
        except MassiveAPIError as exc:
            backfill_note = f"Backfill failed ({exc}). "
            logger.warning("Backfill for %s failed: %s", cfg.symbol, exc)

        result = self.evaluate()
        cfg.is_active = True
        trend = result.extra.get("trend")

        if result.bar_time is None:
            result.message = backfill_note + result.message + " Strategy is ON and will retry on schedule."
        elif result.signal == SELL or (result.signal == HOLD and trend == DOWN):
            result.message = backfill_note + "Downtrend: no entry. Strategy is ON, waiting for the next BUY crossover."
            cfg.last_signal_bar = result.bar_time
        else:
            self._trade(result, BUY, entry=True)
            cfg.last_signal_bar = result.bar_time
            result.message = backfill_note + "Entry: " + (result.message or "")

        self._record(result)
        return result

    def _trade(self, result: StrategyResult, action: str, entry: bool = False) -> None:
        from django.conf import settings

        from services.snaptrade_service import SnapTradeServiceError, TradingDisabledError
        from trading.models import TradeLog

        cfg = self.config
        account = cfg.account
        if account is None or not account.is_linked:
            result.message = f"{action} signal, but no linked brokerage account is set on this strategy."
            return

        quantity = Decimal(cfg.trade_quantity)
        log = TradeLog.objects.create(
            account=account,
            strategy=cfg,
            symbol=cfg.symbol,
            action=action,
            quantity=quantity,
            price=Decimal(str(round(result.close, 4))) if result.close is not None else None,
            status=TradeLog.Status.PENDING,
        )
        result.trade_log_id = log.id

        def finish(status, message):
            log.status, log.message = status, message[:2000]
            log.save()
            result.trade_status, result.message = log.status, log.message

        if not settings.TRADING_ENABLED:
            return finish(TradeLog.Status.SKIPPED, "Dry run: TRADING_ENABLED is False.")

        try:
            if action == SELL or entry:
                held = self.service.get_position_units(
                    account.snaptrade_user_id,
                    account.snaptrade_user_secret,
                    account.account_id,
                    cfg.symbol,
                )
                if entry and held > 0:
                    return finish(TradeLog.Status.SKIPPED, f"Already holding {held:g} {cfg.symbol}; no entry buy.")
                if action == SELL and held <= 0:
                    return finish(TradeLog.Status.SKIPPED, "SELL signal but no position held; not shorting.")
                if action == SELL and held < float(quantity):
                    quantity = Decimal(str(held))
                    log.quantity = quantity

            order = self.service.execute_order(
                user_id=account.snaptrade_user_id,
                user_secret=account.snaptrade_user_secret,
                account_id=account.account_id,
                symbol=cfg.symbol,
                side=action,
                units=quantity,
                order_type="Market",
                is_paper=account.is_paper,
            )
            log.order_id = order["order_id"]
            if order.get("price"):
                log.price = Decimal(str(order["price"]))
            finish(TradeLog.Status.EXECUTED, f"Broker status: {order.get('status') or 'submitted'}")
        except TradingDisabledError as exc:
            finish(TradeLog.Status.SKIPPED, str(exc))
        except SnapTradeServiceError as exc:
            logger.error("Order failed for %s: %s", cfg, exc)
            finish(TradeLog.Status.FAILED, str(exc))


def run_strategy(config, execute: bool = True) -> StrategyResult:
    return MomentumStrategy(config).run(execute=execute)


def start_strategy(config, snaptrade_service=None) -> StrategyResult:
    """Backfill, evaluate, switch ON and (trend permitting) enter a new strategy."""
    return MomentumStrategy(config, snaptrade_service=snaptrade_service).start()


def refresh_and_run_active(backfill_days: int = 7, execute: bool = True, log=print) -> list[StrategyResult]:
    """Refresh recent candles for every active symbol and run all active strategies."""
    from services.massive_service import MassiveAPIError, fetch_historical_data
    from trading.models import StrategyConfig

    strategies = list(StrategyConfig.objects.filter(is_active=True).select_related("account"))
    end = timezone.now().date()
    for symbol in sorted({s.symbol for s in strategies}):
        try:
            fetch_historical_data(symbol, end - timedelta(days=backfill_days), end)
        except MassiveAPIError as exc:
            log(f"Backfill {symbol} failed: {exc}")
    results = []
    for strategy in strategies:
        try:
            result = run_strategy(strategy, execute=execute)
        except Exception as exc:  # keep going with other strategies
            logger.exception("Strategy %s failed", strategy)
            log(f"[{strategy.name}] error: {exc}")
            continue
        log(str(result))
        results.append(result)
    return results
