"""Intraday day-trading engine (see day_trading_strategy.md).

One `run_cycle()` call = one monitoring loop (the scheduler calls it every 1-5 minutes
during market hours):

    0. Safety: at/after the liquidation time (default 3:45 PM ET) sell everything,
       cancel pending buys and clear the observe list. Nothing is held overnight.
    1. Reconcile pending orders (limit buys fill, time out and get cancelled, ...).
    2. Case 2 - Current holdings: stop-loss, exit signal, pyramiding add-on, momentum warning.
    3. Case 3 - Observe list: expire unowned entries, confirmation buys.
    4. Case 1 - Morning screener: once per day, scan the auto New Stock List.

When TRADING_ENABLED is False the engine runs in dry-run mode: orders are not sent to the
broker; fills are simulated at the limit price (buys) or last price (sells), so the whole flow
(holdings, stops, end-of-day liquidation) can be watched safely.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pandas as pd
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from services import indicators as ind
from services.errors import BrokerError

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")  # plain common-stock tickers (no warrants/units/classes)
CENT = Decimal("0.01")


# ---------------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------------
@dataclass
class Bars:
    closed: pd.Series          # closes of completed bars only (signals use these)
    last_price: float | None   # latest price, including the still-forming bar


class MassiveData:
    """Market data from Massive: intraday bars, daily closes and the screener."""

    def __init__(self, client=None):
        if client is None:
            from services.massive_service import MassiveClient

            client = MassiveClient()
        self.client = client

    def intraday(self, symbol: str, minutes: int, now: datetime) -> Bars:
        bars = self.client.get_intraday_bars(symbol, minutes)
        if not bars:
            return Bars(pd.Series(dtype=float), None)
        cutoff = now - timedelta(minutes=minutes)
        closed = [b for b in bars if b.timestamp <= cutoff]
        series = pd.Series([float(b.close) for b in closed], index=[b.timestamp for b in closed], dtype=float)
        return Bars(series, float(bars[-1].close))

    def daily_closes(self, symbol: str, now: datetime) -> pd.Series:
        today = now.astimezone(NEW_YORK).date()
        bars = self.client.get_aggregates(symbol, today - timedelta(days=120), today)
        done = [b for b in bars if b.timestamp.astimezone(NEW_YORK).date() < today]  # completed days only
        return pd.Series([float(b.close) for b in done], dtype=float)

    def screener(self, source: str, watchlist: list[str] | None = None) -> list[dict]:
        if source == "WATCHLIST":
            return watchlist_rows(watchlist)
        if source == "MOST_ACTIVE":
            return self.client.get_all_snapshots()
        return self.client.get_gainers()


def watchlist_rows(symbols) -> list[dict]:
    """Rows for a manual watchlist (price is read from the bars later; filters are skipped)."""
    return [{"symbol": s, "price": None, "volume": None, "change_pct": None} for s in symbols or []]


def parse_watchlist(text: str) -> list[str]:
    """'spy, aapl  NVDA;tsla' -> ['SPY', 'AAPL', 'NVDA', 'TSLA'] (de-duplicated, order kept)."""
    seen = []
    for token in re.split(r"[\s,;]+", (text or "").upper()):
        if token and token not in seen:
            seen.append(token)
    return seen


def get_market_data(cfg=None):
    """Data provider chosen on the Strategy page: Alpaca (free IEX) or Massive (paid)."""
    from trading.models import DayTradeSettings

    cfg = cfg or DayTradeSettings.load()
    if cfg.data_provider == DayTradeSettings.Provider.MASSIVE:
        return MassiveData()
    from services.alpaca_data import AlpacaData

    return AlpacaData()


# ---------------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------------
class SimBroker:
    """Dry run: every order 'fills' immediately; nothing is sent anywhere."""

    simulated = True

    def quote_ask(self, symbol):
        return None

    def held_symbols(self) -> set[str]:
        return set()

    def place(self, symbol, side, quantity, order_type, limit_price=None, ref_price=None):
        price = limit_price if limit_price is not None else ref_price
        return {"order_id": "", "status": "EXECUTED", "filled": True, "price": price}

    def status(self, order_id):  # pragma: no cover - simulated orders are never pending
        return {"status": "EXECUTED", "filled_qty": 0, "price": None}

    def cancel(self, order_id):  # pragma: no cover
        return {}


class LiveBroker:
    """Alpaca through SnapTrade."""

    simulated = False

    def __init__(self, account, service=None):
        if service is None:
            from services.snaptrade_service import SnapTradeService

            service = SnapTradeService()
        self.account = account
        self.service = service

    def _creds(self):
        a = self.account
        return a.snaptrade_user_id, a.snaptrade_user_secret, a.account_id

    def quote_ask(self, symbol):
        try:
            return self.service.get_quotes(*self._creds(), [symbol]).get(symbol.upper(), {}).get("ask")
        except BrokerError as exc:
            logger.warning("Quote for %s failed: %s", symbol, exc)
            return None

    def held_symbols(self) -> set[str]:
        return {
            str(p["symbol"]).upper()
            for p in self.service.get_positions(*self._creds())
            if (p.get("units") or 0) > 0
        }

    def place(self, symbol, side, quantity, order_type, limit_price=None, ref_price=None):
        user_id, user_secret, account_id = self._creds()
        order = self.service.execute_order(
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
            symbol=symbol,
            side=side,
            units=float(quantity),
            order_type=order_type,
            limit_price=float(limit_price) if limit_price is not None else None,
            is_paper=self.account.is_paper,
        )
        filled = str(order.get("status", "")).upper() in {"EXECUTED", "FILLED"}
        return {**order, "filled": filled}

    def status(self, order_id):
        return self.service.get_order_status(*self._creds(), order_id)

    def cancel(self, order_id):
        return self.service.cancel_order(*self._creds(), order_id)


# ---------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------
@dataclass
class CycleReport:
    actions: list[str] = field(default_factory=list)

    def add(self, text: str):
        logger.info(text)
        self.actions.append(text)

    def __str__(self):
        return "\n".join(self.actions) if self.actions else "No actions."


class DayTrader:
    def __init__(self, config=None, data=None, broker=None, now: datetime | None = None):
        from trading.models import DayTradeSettings

        self.cfg = config or DayTradeSettings.load()
        self.now = (now or timezone.now()).astimezone(NEW_YORK)
        self._data = data
        self._broker = broker
        self._bars: dict[str, Bars] = {}
        self.report = CycleReport()

    # -- dependencies -------------------------------------------------------------
    @property
    def data(self):
        if self._data is None:
            self._data = get_market_data(self.cfg)
        return self._data

    @property
    def broker(self):
        if self._broker is None:
            if django_settings.TRADING_ENABLED and self.cfg.account and self.cfg.account.is_linked:
                self._broker = LiveBroker(self.cfg.account)
            else:
                self._broker = SimBroker()
        return self._broker

    @property
    def simulated(self) -> bool:
        return getattr(self.broker, "simulated", False)

    # -- time helpers -------------------------------------------------------------
    def market_open(self) -> bool:
        from datetime import time as dtime

        t = self.now.time()
        return self.now.weekday() < 5 and dtime(9, 30) <= t < dtime(16, 0)

    def past_liquidation(self) -> bool:
        return self.now.time() >= self.cfg.liquidate_at

    def bars(self, symbol: str) -> Bars:
        if symbol not in self._bars:
            self._bars[symbol] = self.data.intraday(symbol, self.cfg.timeframe_minutes, self.now)
        return self._bars[symbol]

    # -- main loop ------------------------------------------------------------------
    def run_cycle(self, force: bool = False) -> CycleReport:
        """One monitoring loop. `force` runs even outside market hours (for testing)."""
        if not force and not self.market_open():
            self.report.add("Market closed: nothing to do.")
            return self.report

        if self.past_liquidation():
            self.liquidate_all()
        else:
            self.reconcile_orders()
            self.prefetch(self._tracked_symbols())
            self.monitor_holdings()
            self.process_observe_list()
            if self.cfg.last_scan_at is None or self.cfg.last_scan_at.astimezone(NEW_YORK).date() != self.now.date():
                self.scan()
        self.cfg.last_cycle_at = timezone.now()
        self.cfg.save(update_fields=["last_cycle_at"])
        return self.report

    def _tracked_symbols(self) -> list[str]:
        from trading.models import ObserveItem, Position

        held = Position.objects.filter(status__in=["PENDING", "OPEN"]).values_list("symbol", flat=True)
        return sorted(set(held) | set(ObserveItem.objects.values_list("symbol", flat=True)))

    def prefetch(self, symbols, daily: bool = False):
        """Batch-load bars for many symbols in one request when the provider supports it."""
        symbols = [s for s in symbols if s not in self._bars]
        if not symbols:
            return
        try:
            if hasattr(self.data, "intraday_many"):
                self._bars.update(self.data.intraday_many(symbols, self.cfg.timeframe_minutes, self.now))
            if daily and hasattr(self.data, "daily_many"):
                self.data.daily_many(symbols, self.now)
        except Exception as exc:  # fall back to per-symbol requests
            self.report.add(f"Batch price request failed ({exc}); fetching one by one.")

    # -- Case 1: screener ---------------------------------------------------------------
    def candidates(self) -> list[dict]:
        """Today's New Stock List from the screener (or the manual watchlist)."""
        cfg = self.cfg
        watchlist = parse_watchlist(cfg.watchlist)
        source = cfg.screener_source
        try:
            raw = self.data.screener(source, watchlist=watchlist)
        except Exception as exc:
            if source == "WATCHLIST" or not watchlist:
                raise
            self.report.add(f"Screener failed ({exc}); using your watchlist instead.")
            source, raw = "WATCHLIST", self.data.screener("WATCHLIST", watchlist=watchlist)
        if not raw and source != "WATCHLIST" and watchlist:
            self.report.add("Screener returned nothing; using your watchlist instead.")
            source, raw = "WATCHLIST", self.data.screener("WATCHLIST", watchlist=watchlist)

        if source == "WATCHLIST":  # you picked these: no price/volume filters, keep your order
            return [r for r in raw if SYMBOL_RE.match(r["symbol"])][: max(cfg.screener_count, len(watchlist))]
        rows = [
            r for r in raw
            if SYMBOL_RE.match(r["symbol"])
            and float(cfg.min_price) <= (r["price"] or 0) <= float(cfg.max_price)
            and (r["volume"] or 0) >= cfg.min_volume
        ]
        key = (lambda r: r["volume"]) if source == "MOST_ACTIVE" else (lambda r: r["change_pct"])
        return sorted(rows, key=key, reverse=True)[: cfg.screener_count]

    def scan(self) -> list:
        """Case 1: evaluate today's New Stock List."""
        from trading.models import ObserveItem, Position, ScanResult

        cfg = self.cfg
        cfg.last_scan_at = timezone.now()
        cfg.save(update_fields=["last_scan_at"])
        try:
            candidates = self.candidates()
        except Exception as exc:
            self.report.add(f"Screener failed: {exc}")
            return []
        self.prefetch([r["symbol"] for r in candidates], daily=True)
        held = set(Position.objects.filter(status__in=["PENDING", "OPEN"]).values_list("symbol", flat=True))
        try:
            held |= self.broker.held_symbols()
        except BrokerError as exc:
            self.report.add(f"Could not read broker holdings: {exc}")

        results = []
        for row in candidates:
            sym = row["symbol"]
            result = ScanResult(
                symbol=sym, price=_dec(row["price"]), change_pct=_dec(row["change_pct"]), volume=row["volume"],
            )
            try:
                result.outcome, result.note = self._evaluate_candidate(sym, row, held, result)
            except Exception as exc:  # one bad ticker must not stop the scan
                result.outcome, result.note = ScanResult.Outcome.SKIPPED, f"Error: {exc}"[:255]
            result.save()
            results.append(result)
        self.report.add(f"Scanned {len(results)} candidates: " + ", ".join(f"{r.symbol}={r.outcome}" for r in results))
        return results

    def _evaluate_candidate(self, sym, row, held, result):
        from trading.models import ObserveItem, ScanResult

        if sym in held:
            return ScanResult.Outcome.HELD, "Already in current holdings."
        bars = self.bars(sym)
        price = bars.last_price or row["price"]
        if not price:
            return ScanResult.Outcome.SKIPPED, "No price data."
        result.price = result.price or _dec(price)
        passes, sma50 = ind.above_daily_sma(self.data.daily_closes(sym, self.now), price)
        result.daily_sma50 = _dec(sma50)
        if not passes:
            note = f"Price {price:.2f} below daily 50 SMA {sma50:.2f}." if sma50 else "Not enough daily history."
            return ScanResult.Outcome.BELOW_SMA, note
        snap = ind.snapshot(bars.closed)
        if snap is None:
            return ScanResult.Outcome.SKIPPED, f"Not enough {self.cfg.timeframe_minutes}-min bars yet."
        outcome = snap.classify_new()
        if outcome == "BUY":
            if not self._can_open_new():
                return ScanResult.Outcome.SKIPPED, "Buy signal, but max open positions reached."
            self.enter(sym, price, reason="ENTRY")
            return ScanResult.Outcome.BUY, snap.describe()
        if outcome == "OBSERVE":
            ObserveItem.objects.update_or_create(
                symbol=sym, defaults={"reason": ObserveItem.Reason.DEVELOPING, "added_at": timezone.now(), "note": snap.describe()[:255]},
            )
            return ScanResult.Outcome.OBSERVE, snap.describe()
        return ScanResult.Outcome.DISCARD, snap.describe()

    def _can_open_new(self) -> bool:
        from trading.models import Position

        if self.past_liquidation():
            return False
        return Position.objects.filter(status__in=["PENDING", "OPEN"]).count() < self.cfg.max_open_positions

    # -- Case 2: holdings ---------------------------------------------------------------
    def monitor_holdings(self):
        from trading.models import ObserveItem, Position, TradeLog

        for pos in Position.objects.filter(status=Position.Status.OPEN):
            if pos.orders.filter(status=TradeLog.Status.PENDING).exists():
                continue  # wait for the in-flight order first
            try:
                bars = self.bars(pos.symbol)
            except Exception as exc:
                self.report.add(f"{pos.symbol}: price data failed ({exc})")
                continue
            if bars.last_price is not None:
                pos.last_price = _dec(bars.last_price)
                pos.save(update_fields=["last_price"])
                if pos.last_price <= pos.stop_price:
                    self.exit(pos, "STOP_LOSS", f"Price {bars.last_price:.2f} hit stop {pos.stop_price}")
                    continue
            snap = ind.snapshot(bars.closed)
            if snap is None:
                continue
            if snap.exit_signal:
                why = "9 EMA crossed below 21 EMA" if snap.cross_down else "MACD histogram negative"
                self.exit(pos, "EXIT_SIGNAL", why)
                continue
            if snap.add_on_signal and pos.blocks < self.cfg.max_blocks:
                self.enter(pos.symbol, bars.last_price or snap.close, reason="ADD_ON", position=pos)
            if snap.momentum_warning:
                ObserveItem.objects.update_or_create(
                    symbol=pos.symbol,
                    defaults={"reason": ObserveItem.Reason.WARNING, "note": f"Momentum fading: {snap.describe()}"[:255]},
                )

    # -- Case 3: observe list ------------------------------------------------------------
    def process_observe_list(self):
        from trading.models import ObserveItem, Position

        active = set(Position.objects.filter(status__in=["PENDING", "OPEN"]).values_list("symbol", flat=True))
        for item in ObserveItem.objects.all():
            owned = item.symbol in active
            if owned:
                continue  # owned tickers are exited by the Case 2 rules (stop-loss / bearish cross)
            if item.reason == ObserveItem.Reason.WARNING:
                item.delete()  # the flagged holding has been closed
                continue
            age = (timezone.now() - item.added_at).total_seconds() / 60
            if age > self.cfg.observe_minutes:
                self.report.add(f"{item.symbol}: removed from observe list (expired after {int(age)} min).")
                item.delete()
                continue
            try:
                bars = self.bars(item.symbol)
            except Exception as exc:
                self.report.add(f"{item.symbol}: price data failed ({exc})")
                continue
            snap = ind.snapshot(bars.closed)
            if snap and snap.confirmation_buy and self._can_open_new():
                self.enter(item.symbol, bars.last_price or snap.close, reason="ENTRY")

    # -- orders ---------------------------------------------------------------------------
    def enter(self, symbol: str, last_price: float, reason: str, position=None):
        """Limit buy one block at ask + offset."""
        from trading.models import Position, TradeLog

        cfg = self.cfg
        ask = self._ask_price(symbol, last_price)
        limit = (Decimal(str(ask)) + Decimal(str(cfg.limit_offset))).quantize(CENT, rounding=ROUND_HALF_UP)
        qty = math.floor(float(cfg.block_usd) / float(limit)) if limit > 0 else 0
        if qty < 1:
            self.report.add(f"{symbol}: block ${cfg.block_usd} is less than one share at {limit}; no order.")
            return None
        with transaction.atomic():
            if position is None:
                position = Position.objects.create(
                    account=cfg.account, symbol=symbol, simulated=self.simulated, last_price=_dec(last_price)
                )
            log = TradeLog.objects.create(
                account=cfg.account, position=position, symbol=symbol, action="BUY", reason=reason,
                order_type="Limit", quantity=qty, limit_price=limit,
            )
        self._send(log, ref_price=last_price)
        return log

    def _ask_price(self, symbol: str, last_price: float) -> float:
        """Ask from the data provider, then the broker, then the last price. An ask more than 2%
        away from the last price (thin IEX quote) is ignored."""
        for source in (getattr(self.data, "quote_ask", None), self.broker.quote_ask):
            if source is None:
                continue
            try:
                ask = source(symbol)
            except Exception:
                ask = None
            if ask and (not last_price or abs(ask / last_price - 1) <= 0.02):
                return ask
        return last_price

    def exit(self, pos, reason: str, why: str = ""):
        """Market sell the whole bot position."""
        from trading.models import TradeLog

        log = TradeLog.objects.create(
            account=pos.account, position=pos, symbol=pos.symbol, action="SELL", reason=reason,
            order_type="Market", quantity=pos.quantity, message=why,
        )
        self._send(log, ref_price=float(pos.last_price or pos.avg_price))
        return log

    def _send(self, log, ref_price):
        from services.errors import OrderSkipped, TradingDisabledError
        from trading.models import TradeLog

        try:
            result = self.broker.place(
                log.symbol, log.action, log.quantity, log.order_type,
                limit_price=log.limit_price, ref_price=ref_price,
            )
        except (TradingDisabledError, OrderSkipped) as exc:
            self._fail(log, TradeLog.Status.SKIPPED, str(exc))
            return
        except BrokerError as exc:
            self._fail(log, TradeLog.Status.FAILED, str(exc))
            return
        log.order_id = result.get("order_id") or ""
        if result.get("filled"):
            status = TradeLog.Status.SIMULATED if self.simulated else TradeLog.Status.EXECUTED
            self._apply_fill(log, log.quantity, result.get("price") or log.limit_price or ref_price, status)
        else:
            log.save(update_fields=["order_id"])
            self.report.add(f"{log.action} {log.quantity} {log.symbol} submitted ({log.get_reason_display()}).")

    def _fail(self, log, status, message):
        from trading.models import Position

        log.status, log.message = status, (log.message + " " + message).strip()[:2000]
        log.save()
        pos = log.position
        if log.action == "BUY" and pos and pos.status == Position.Status.PENDING and not pos.quantity:
            pos.status = Position.Status.CANCELED
            pos.save(update_fields=["status"])
        self.report.add(f"{log.action} {log.symbol} {status.lower()}: {message}")

    def _apply_fill(self, log, qty, price, status):
        from trading.models import ObserveItem, Position

        qty, price = Decimal(str(qty)), Decimal(str(price))
        log.status, log.filled_quantity, log.price = status, qty, price
        log.save()
        pos = log.position
        if log.action == "BUY":
            total = pos.quantity + qty
            pos.avg_price = ((pos.avg_price * pos.quantity + price * qty) / total).quantize(Decimal("0.0001"))
            pos.quantity = total
            pos.blocks += 1
            pos.status = Position.Status.OPEN
            stop_pct = Decimal(str(self.cfg.stop_loss_pct))
            pos.stop_price = (pos.avg_price * (1 - stop_pct / 100)).quantize(Decimal("0.0001"))
            pos.last_price = pos.last_price or price
            pos.save()
            ObserveItem.objects.filter(symbol=pos.symbol, reason=ObserveItem.Reason.DEVELOPING).delete()
            self.report.add(f"BUY {qty} {pos.symbol} @ {price} ({log.get_reason_display()}, block {pos.blocks}).")
        else:
            pos.status = Position.Status.CLOSED
            pos.exit_price, pos.closed_at, pos.exit_reason = price, timezone.now(), log.get_reason_display()
            pos.save()
            ObserveItem.objects.filter(symbol=pos.symbol).delete()
            self.report.add(f"SELL {qty} {pos.symbol} @ {price} ({log.get_reason_display()}).")

    def reconcile_orders(self):
        """Check in-flight broker orders: apply fills, cancel stale limit buys."""
        from trading.models import TradeLog

        for log in TradeLog.objects.filter(status=TradeLog.Status.PENDING).exclude(order_id="").select_related("position"):
            try:
                st = self.broker.status(log.order_id)
            except BrokerError as exc:
                self.report.add(f"Order {log.order_id} status check failed: {exc}")
                continue
            status, filled = st.get("status", ""), Decimal(str(st.get("filled_qty") or 0))
            age = (timezone.now() - log.created_at).total_seconds() / 60
            if status in {"EXECUTED", "FILLED"}:
                self._apply_fill(log, filled or log.quantity, st.get("price") or log.limit_price, TradeLog.Status.EXECUTED)
            elif status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}:
                if filled:
                    self._apply_fill(log, filled, st.get("price") or log.limit_price, TradeLog.Status.EXECUTED)
                else:
                    self._fail(log, TradeLog.Status.CANCELED, f"Broker status {status}.")
            elif log.action == "BUY" and age >= self.cfg.buy_timeout_minutes:
                try:
                    self.broker.cancel(log.order_id)
                except BrokerError as exc:
                    self.report.add(f"Cancel {log.order_id} failed: {exc}")
                    continue
                if filled:
                    self._apply_fill(log, filled, st.get("price") or log.limit_price, TradeLog.Status.EXECUTED)
                else:
                    self._fail(log, TradeLog.Status.CANCELED, f"Limit buy not filled within {self.cfg.buy_timeout_minutes} min; cancelled.")

    # -- end of day --------------------------------------------------------------------------
    def liquidate_all(self, reason: str = "EOD"):
        """Sell every bot position, cancel pending buys and clear the observe list."""
        from trading.models import ObserveItem, Position, TradeLog

        for log in TradeLog.objects.filter(status=TradeLog.Status.PENDING, action="BUY").exclude(order_id=""):
            try:
                self.broker.cancel(log.order_id)
            except BrokerError as exc:
                self.report.add(f"Cancel {log.order_id} failed: {exc}")
            self._fail(log, TradeLog.Status.CANCELED, "Cancelled at liquidation time.")
        positions = list(Position.objects.filter(status=Position.Status.OPEN))
        for pos in positions:
            if pos.orders.filter(status=TradeLog.Status.PENDING, action="SELL").exists():
                continue
            try:
                bars = self.bars(pos.symbol)
                if bars.last_price is not None:
                    pos.last_price = _dec(bars.last_price)
            except Exception:
                pass
            label = "End-of-day liquidation" if reason == "EOD" else "Manual close-all"
            self.exit(pos, reason, label)
        Position.objects.filter(status=Position.Status.PENDING, quantity=0).update(status=Position.Status.CANCELED)
        ObserveItem.objects.all().delete()
        if reason == "EOD":
            self.cfg.last_liquidation_date = self.now.date()
            self.cfg.save(update_fields=["last_liquidation_date"])
        if positions:
            self.report.add(f"Liquidated {len(positions)} position(s) ({reason}).")


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(float(value), 4)))
