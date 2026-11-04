from datetime import datetime, time as dtime, timedelta, timezone as dt_tz
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from services import indicators as ind
from services.daytrader import Bars, DayTrader, SimBroker
from services.massive_service import Candle, MassiveAPIError, MassiveClient, _snapshot_row, fetch_historical_data
from services.snaptrade_service import SnapTradeService, SnapTradeServiceError, TradingDisabledError

from .models import BrokerageAccount, DayTradeSettings, ObserveItem, Position, ScanResult, StockData, TradeLog

NY = ZoneInfo("America/New_York")
START = datetime(2025, 1, 1, tzinfo=dt_tz.utc)
MONDAY_1030 = datetime(2026, 11, 2, 10, 30, tzinfo=NY)


def path(seed):
    """Deterministic intraday path: a gentle decline followed by a noisy rise."""
    r = np.random.default_rng(seed)
    down = np.linspace(100, 96, 45) + r.normal(0, 0.08, 45)
    up = 96 + np.cumsum(r.normal(0.04, 0.12, 80))
    return np.r_[down, up]


def closes(seed, n):
    return pd.Series(path(seed)[:n])


# Prefixes of the paths above that trigger each rule on the last bar (found by search).
BUY = (2, 58)          # fresh 9/21 cross + RSI 45-65 + expanding MACD
DEVELOPING = (0, 56)   # uptrend, momentum not confirmed yet
CONFIRM = (0, 67)      # same path later: confirmation buy
WARNING = (0, 62)      # momentum warning
EXIT = (1, 64)         # 9 EMA crosses below 21 EMA
ADD_ON = (1, 68)       # RSI bounce off 50 + expanding MACD


class FakeData:
    """Market data the tests control."""

    def __init__(self):
        self.series = {}
        self.last = {}
        self.daily = {}
        self.rows = []
        self.calls = 0

    def set(self, symbol, spec, last=None, daily_ok=True):
        s = closes(*spec)
        self.series[symbol] = s
        self.last[symbol] = float(s.iloc[-1]) if last is None else last
        self.daily[symbol] = pd.Series([50.0] * 60) if daily_ok else pd.Series([500.0] * 60)

    def intraday(self, symbol, minutes, now):
        self.calls += 1
        return Bars(self.series.get(symbol, pd.Series(dtype=float)), self.last.get(symbol))

    def daily_closes(self, symbol, now):
        return self.daily.get(symbol, pd.Series(dtype=float))

    def screener(self, source):
        return self.rows


class FakeLiveBroker:
    """A non-simulated broker whose orders stay pending until the test says otherwise."""

    simulated = False

    def __init__(self, held=()):
        self.orders, self.cancelled, self.held = [], [], set(held)
        self.state = {}

    def quote_ask(self, symbol):
        return 100.00

    def held_symbols(self):
        return self.held

    def place(self, symbol, side, quantity, order_type, limit_price=None, ref_price=None):
        oid = f"ord-{len(self.orders) + 1}"
        self.orders.append({"id": oid, "symbol": symbol, "side": side, "qty": quantity, "type": order_type, "limit": limit_price})
        self.state[oid] = {"status": "PENDING", "filled_qty": 0, "price": None}
        return {"order_id": oid, "status": "PENDING", "filled": False}

    def status(self, order_id):
        return self.state[order_id]

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        self.state[order_id] = {"status": "CANCELED", "filled_qty": 0, "price": None}


def trader(data, now=MONDAY_1030, broker=None, **cfg):
    config = DayTradeSettings.load()
    for k, v in cfg.items():
        setattr(config, k, v)
    config.save()
    return DayTrader(config=config, data=data, broker=broker or SimBroker(), now=now)


def open_position(symbol="AAA", qty=10, avg="97.00", blocks=1, stop_pct=Decimal("1.5")):
    avg = Decimal(avg)
    return Position.objects.create(
        symbol=symbol, status="OPEN", simulated=True, blocks=blocks, quantity=qty, avg_price=avg,
        stop_price=(avg * (1 - stop_pct / 100)).quantize(Decimal("0.0001")), last_price=avg,
    )


# ---------------------------------------------------------------------------------
class IndicatorTests(TestCase):
    def test_rule_prefixes(self):
        self.assertTrue(ind.snapshot(closes(*BUY)).buy_signal)
        s = ind.snapshot(closes(*DEVELOPING))
        self.assertTrue(s.developing and not s.confirmation_buy)
        self.assertEqual(s.classify_new(), "OBSERVE")
        self.assertTrue(ind.snapshot(closes(*CONFIRM)).confirmation_buy)
        self.assertTrue(ind.snapshot(closes(*WARNING)).momentum_warning)
        s = ind.snapshot(closes(*EXIT))
        self.assertTrue(s.exit_signal and s.cross_down)
        self.assertTrue(ind.snapshot(closes(*ADD_ON)).add_on_signal)

    def test_rsi_and_daily_filter(self):
        self.assertEqual(ind.rsi(pd.Series([100.0] * 30)).iloc[-1], 50.0)
        self.assertEqual(ind.rsi(pd.Series(np.arange(30, dtype=float))).iloc[-1], 100.0)
        self.assertEqual(ind.above_daily_sma(pd.Series(range(1, 61), dtype=float), 70), (True, 35.5))
        self.assertEqual(ind.above_daily_sma(pd.Series([1.0] * 10), 70), (False, None))
        self.assertIsNone(ind.snapshot(pd.Series([1.0] * 10)))


class ScreenerScanTests(TestCase):
    """Case 1: morning screener."""

    def test_candidate_filters_and_ranking(self):
        data = FakeData()
        data.rows = [
            {"symbol": "AAA", "price": 50, "volume": 2_000_000, "change_pct": 5},
            {"symbol": "BBB", "price": 3, "volume": 9_000_000, "change_pct": 30},       # too cheap
            {"symbol": "CCC", "price": 60, "volume": 10_000, "change_pct": 20},         # too little volume
            {"symbol": "DDD.WS", "price": 60, "volume": 5_000_000, "change_pct": 25},   # warrant
            {"symbol": "EEE", "price": 80, "volume": 3_000_000, "change_pct": 9},
        ]
        t = trader(data, screener_count=5)
        self.assertEqual([r["symbol"] for r in t.candidates()], ["EEE", "AAA"])
        t.cfg.screener_source = "MOST_ACTIVE"
        self.assertEqual([r["symbol"] for r in t.candidates()], ["EEE", "AAA"])
        t.cfg.screener_count = 1
        self.assertEqual(len(t.candidates()), 1)

    def test_scan_outcomes(self):
        data = FakeData()
        data.set("BUYME", BUY)
        data.set("WATCH", DEVELOPING)
        data.set("LOWER", BUY, daily_ok=False)
        data.set("BEAR", EXIT)
        data.rows = [{"symbol": s, "price": 97, "volume": 5_000_000, "change_pct": 5 - i}
                     for i, s in enumerate(["BUYME", "WATCH", "LOWER", "BEAR", "HELD"])]
        open_position("HELD")
        t = trader(data)
        t.scan()
        outcomes = dict(ScanResult.objects.values_list("symbol", "outcome"))
        self.assertEqual(outcomes, {"BUYME": "BUY", "WATCH": "OBSERVE", "LOWER": "BELOW_SMA", "BEAR": "DISCARD", "HELD": "HELD"})

        pos = Position.objects.get(symbol="BUYME")
        last = data.last["BUYME"]
        limit = Decimal(str(round(last + 0.02, 2)))
        self.assertEqual((pos.status, pos.blocks, pos.simulated), ("OPEN", 1, True))
        self.assertEqual(pos.quantity, Decimal(int(1000 // float(limit))))
        self.assertEqual(pos.avg_price, limit)
        self.assertEqual(pos.stop_price, (limit * Decimal("0.985")).quantize(Decimal("0.0001")))
        log = pos.orders.get()
        self.assertEqual((log.status, log.order_type, log.reason), ("SIMULATED", "Limit", "ENTRY"))
        self.assertTrue(ObserveItem.objects.filter(symbol="WATCH", reason="DEVELOPING").exists())
        self.assertFalse(ObserveItem.objects.filter(symbol="BEAR").exists())

    def test_scan_runs_once_per_day_in_cycle(self):
        data = FakeData()
        t = trader(data)
        with mock.patch.object(DayTrader, "scan") as scan:
            t.run_cycle()
            self.assertEqual(scan.call_count, 1)
        t.cfg.last_scan_at = timezone.now()
        t.cfg.save()
        with mock.patch.object(DayTrader, "scan") as scan:
            trader(data, now=timezone.now()).run_cycle(force=True)
            scan.assert_not_called()

    def test_max_open_positions(self):
        data = FakeData()
        data.set("NEW", BUY)
        data.rows = [{"symbol": "NEW", "price": 97, "volume": 5_000_000, "change_pct": 5}]
        open_position("X1")
        trader(data, max_open_positions=1).scan()
        self.assertEqual(ScanResult.objects.get().outcome, "SKIPPED")
        self.assertFalse(Position.objects.filter(symbol="NEW").exists())


class HoldingsTests(TestCase):
    """Case 2: monitoring current holdings."""

    def test_stop_loss(self):
        data = FakeData()
        data.set("AAA", DEVELOPING, last=95.0)  # 97 * 0.985 = 95.545 -> stop hit
        pos = open_position("AAA")
        trader(data).monitor_holdings()
        pos.refresh_from_db()
        self.assertEqual((pos.status, pos.exit_reason), ("CLOSED", "Stop-loss"))
        self.assertEqual(pos.exit_price, Decimal("95"))
        self.assertEqual(pos.orders.get().order_type, "Market")

    def test_exit_signal(self):
        data = FakeData()
        data.set("AAA", EXIT)
        pos = open_position("AAA", avg="90")
        trader(data).monitor_holdings()
        pos.refresh_from_db()
        self.assertEqual((pos.status, pos.exit_reason), ("CLOSED", "Exit signal"))

    def test_add_on_respects_block_cap(self):
        data = FakeData()
        data.set("AAA", ADD_ON)
        pos = open_position("AAA", avg="90")
        trader(data).monitor_holdings()
        pos.refresh_from_db()
        self.assertEqual(pos.blocks, 2)
        self.assertGreater(pos.quantity, 10)
        trader(data).monitor_holdings()  # cap reached: no third block
        pos.refresh_from_db()
        self.assertEqual(pos.blocks, 2)
        self.assertEqual(pos.orders.filter(reason="ADD_ON").count(), 1)

    def test_momentum_warning_flags_observe(self):
        data = FakeData()
        data.set("AAA", WARNING)
        open_position("AAA", avg="90")
        trader(data).monitor_holdings()
        self.assertEqual(ObserveItem.objects.get(symbol="AAA").reason, "WARNING")


class ObserveListTests(TestCase):
    """Case 3: observe list."""

    def test_expiry_of_unowned(self):
        ObserveItem.objects.create(symbol="OLD", added_at=timezone.now() - timedelta(minutes=61))
        ObserveItem.objects.create(symbol="NEW", added_at=timezone.now() - timedelta(minutes=5))
        data = FakeData()
        data.set("NEW", DEVELOPING)
        trader(data).process_observe_list()
        self.assertEqual(list(ObserveItem.objects.values_list("symbol", flat=True)), ["NEW"])

    def test_confirmation_buy_moves_to_holdings(self):
        ObserveItem.objects.create(symbol="AAA")
        data = FakeData()
        data.set("AAA", CONFIRM)
        trader(data).process_observe_list()
        self.assertEqual(Position.objects.get(symbol="AAA").status, "OPEN")
        self.assertFalse(ObserveItem.objects.filter(symbol="AAA").exists())

    def test_owned_warning_kept_then_removed_after_close(self):
        pos = open_position("AAA")
        ObserveItem.objects.create(symbol="AAA", reason="WARNING", added_at=timezone.now() - timedelta(hours=3))
        trader(FakeData()).process_observe_list()
        self.assertTrue(ObserveItem.objects.filter(symbol="AAA").exists())  # owned: no expiry
        pos.status = "CLOSED"
        pos.save()
        ObserveItem.objects.create(symbol="BBB", reason="WARNING")
        trader(FakeData()).process_observe_list()
        self.assertFalse(ObserveItem.objects.filter(symbol="BBB").exists())


class EndOfDayTests(TestCase):
    def test_liquidation_at_345(self):
        data = FakeData()
        data.set("AAA", BUY)
        open_position("AAA")
        open_position("BBB")
        ObserveItem.objects.create(symbol="CCC")
        t = trader(data, now=datetime(2026, 11, 2, 15, 45, tzinfo=NY))
        with mock.patch.object(DayTrader, "scan") as scan:
            t.run_cycle()
            scan.assert_not_called()
        self.assertFalse(Position.objects.filter(status="OPEN").exists())
        self.assertEqual(set(Position.objects.values_list("exit_reason", flat=True)), {"End-of-day liquidation"})
        self.assertFalse(ObserveItem.objects.exists())
        self.assertEqual(t.cfg.last_liquidation_date, datetime(2026, 11, 2).date())

    def test_no_entries_after_liquidation_time(self):
        t = trader(FakeData(), now=datetime(2026, 11, 2, 15, 50, tzinfo=NY))
        self.assertFalse(t._can_open_new())

    def test_closed_market_does_nothing(self):
        open_position("AAA")
        report = trader(FakeData(), now=datetime(2026, 11, 7, 11, 0, tzinfo=NY)).run_cycle()  # Saturday
        self.assertIn("Market closed", str(report))
        self.assertEqual(Position.objects.get().status, "OPEN")

    def test_scheduler_wakes_at_liquidation_time(self):
        from trading.management.commands.run_scheduler import _sleep_seconds, market_is_open

        now = datetime(2026, 11, 2, 15, 44, 0, tzinfo=NY)
        self.assertEqual(_sleep_seconds(now, 2, dtime(15, 45)), 61)
        self.assertEqual(_sleep_seconds(datetime(2026, 11, 2, 11, 0, tzinfo=NY), 2, dtime(15, 45)), 120)
        self.assertTrue(market_is_open(datetime(2026, 11, 2, 14, 45, tzinfo=dt_tz.utc)))   # 09:45 EST
        self.assertFalse(market_is_open(datetime(2026, 11, 2, 14, 15, tzinfo=dt_tz.utc)))  # 09:15 EST


class LiveOrderFlowTests(TestCase):
    """Limit buys at ask + $0.02 that stay pending until the broker reports a fill."""

    def setUp(self):
        self.data = FakeData()
        self.data.set("AAA", BUY)
        self.broker = FakeLiveBroker()

    def test_limit_buy_then_fill(self):
        t = trader(self.data, broker=self.broker)
        t.enter("AAA", 99.5, reason="ENTRY")
        order = self.broker.orders[0]
        self.assertEqual((order["type"], order["limit"], order["qty"]), ("Limit", Decimal("100.02"), 9))
        pos = Position.objects.get()
        self.assertEqual(pos.status, "PENDING")
        self.broker.state["ord-1"] = {"status": "EXECUTED", "filled_qty": 9, "price": 100.01}
        trader(self.data, broker=self.broker).reconcile_orders()
        pos.refresh_from_db()
        self.assertEqual((pos.status, pos.quantity, pos.avg_price), ("OPEN", Decimal("9"), Decimal("100.01")))
        self.assertEqual(pos.orders.get().status, "EXECUTED")

    def test_unfilled_buy_cancelled_after_timeout(self):
        t = trader(self.data, broker=self.broker)
        log = t.enter("AAA", 99.5, reason="ENTRY")
        TradeLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(minutes=6))
        trader(self.data, broker=self.broker).reconcile_orders()
        self.assertEqual(self.broker.cancelled, ["ord-1"])
        self.assertEqual(TradeLog.objects.get().status, "CANCELED")
        self.assertEqual(Position.objects.get().status, "CANCELED")

    def test_broker_holdings_are_skipped(self):
        self.data.rows = [{"symbol": "AAA", "price": 97, "volume": 5_000_000, "change_pct": 5}]
        trader(self.data, broker=FakeLiveBroker(held={"AAA"})).scan()
        self.assertEqual(ScanResult.objects.get().outcome, "HELD")

    def test_eod_cancels_pending_buys(self):
        t = trader(self.data, broker=self.broker)
        t.enter("AAA", 99.5, reason="ENTRY")
        trader(self.data, broker=self.broker, now=datetime(2026, 11, 2, 15, 45, tzinfo=NY)).run_cycle()
        self.assertEqual(self.broker.cancelled, ["ord-1"])
        self.assertEqual(Position.objects.get().status, "CANCELED")

    def test_disabled_trading_is_skipped_not_failed(self):
        class Refusing(FakeLiveBroker):
            def place(self, *a, **k):
                raise TradingDisabledError("TRADING_ENABLED is False")

        trader(self.data, broker=Refusing()).enter("AAA", 99.5, reason="ENTRY")
        self.assertEqual(TradeLog.objects.get().status, "SKIPPED")
        self.assertEqual(Position.objects.get().status, "CANCELED")


@override_settings(SNAPTRADE_CLIENT_ID="", SNAPTRADE_CONSUMER_KEY="")
class ViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("trader", password="pw-123456")
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("trading:dashboard")).status_code, 302)

    def test_pages_render(self):
        open_position("AAA")
        ObserveItem.objects.create(symbol="BBB")
        ScanResult.objects.create(symbol="CCC", outcome="DISCARD", note="bearish")
        for name in ("trading:dashboard", "trading:strategy", "trading:positions"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        html = self.client.get(reverse("trading:dashboard")).content.decode()
        for text in ("AAA", "BBB", "CCC", "Auto day trading OFF", 'id="mkt-clock"'):
            self.assertIn(text, html)

    def test_save_settings_and_toggle(self):
        data = {
            "account": "", "timeframe_minutes": 15, "loop_minutes": 3, "block_usd": "2500", "max_blocks": 2,
            "max_open_positions": 4, "stop_loss_pct": "1.5", "limit_offset": "0.02", "buy_timeout_minutes": 5,
            "liquidate_at": "15:45", "observe_minutes": 60, "screener_source": "MOST_ACTIVE", "screener_count": 8,
            "min_price": "5", "max_price": "300", "min_volume": 2000000,
        }
        self.client.post(reverse("trading:strategy"), data)
        cfg = DayTradeSettings.load()
        self.assertEqual((cfg.timeframe_minutes, cfg.block_usd, cfg.screener_source), (15, Decimal("2500"), "MOST_ACTIVE"))
        response = self.client.post(reverse("trading:strategy"), {**data, "loop_minutes": 10})
        self.assertContains(response, "1–5 minutes")
        self.client.post(reverse("trading:toggle_auto"))
        self.assertTrue(DayTradeSettings.load().auto_trading)

    def test_close_position_and_close_all(self):
        pos = open_position("AAA")
        open_position("BBB")
        with mock.patch("services.daytrader.MassiveData") as md:
            md.return_value.intraday.return_value = Bars(pd.Series(dtype=float), 98.0)
            self.client.post(reverse("trading:position_close", args=[pos.pk]))
            pos.refresh_from_db()
            self.assertEqual((pos.status, pos.exit_reason), ("CLOSED", "Manual"))
            self.client.post(reverse("trading:liquidate_now"))
        self.assertFalse(Position.objects.filter(status="OPEN").exists())

    def test_chart_endpoint(self):
        s = closes(*BUY)
        idx = pd.date_range("2026-11-02 14:30", periods=len(s), freq="5min", tz="UTC")
        with mock.patch("trading.views.MassiveData") as md:
            md.return_value.intraday.return_value = Bars(pd.Series(s.values, index=idx), 97.0)
            data = self.client.get(reverse("trading:chart", args=["aaa"])).json()
        self.assertEqual(data["symbol"], "AAA")
        self.assertEqual(len(data["labels"]), len(s))
        self.assertEqual(data["labels"][0], "11-02 09:30")  # shown in New York time
        self.assertIsNotNone(data["ema_slow"][-1])

    def test_trade_times_in_eastern(self):
        log = TradeLog.objects.create(symbol="AAPL", action="BUY", quantity=1, status="SIMULATED")
        TradeLog.objects.filter(pk=log.pk).update(created_at=datetime(2026, 1, 15, 15, 0, tzinfo=dt_tz.utc))
        self.assertContains(self.client.get(reverse("trading:dashboard")), "Jan 15, 10:00 EST")


class CommandTests(TestCase):
    def test_run_daytrade_scan(self):
        out = StringIO()
        with mock.patch("services.daytrader.MassiveData") as md:
            md.return_value.screener.return_value = []
            call_command("run_daytrade", "--scan", stdout=out)
        self.assertIn("Scanned 0 candidates", out.getvalue())

    def test_run_daytrade_liquidate(self):
        open_position("AAA")
        with mock.patch("services.daytrader.MassiveData") as md:
            md.return_value.intraday.return_value = Bars(pd.Series(dtype=float), 98.0)
            call_command("run_daytrade", "--liquidate", stdout=StringIO())
        self.assertEqual(Position.objects.get().exit_reason, "Manual")


class MassiveIntradayTests(TestCase):
    def test_regular_session_filter_and_snapshot_rows(self):
        def bar(hh, mm):
            ts = datetime(2026, 11, 2, hh, mm, tzinfo=NY).astimezone(dt_tz.utc)
            return Candle("AAA", ts, Decimal(1), Decimal(1), Decimal(1), Decimal(1), 1)

        client = MassiveClient(api_key="k", base_url="https://x", session=mock.Mock())
        with mock.patch.object(MassiveClient, "get_aggregates", return_value=[bar(8, 0), bar(9, 30), bar(15, 55), bar(16, 0)]):
            got = client.get_intraday_bars("AAA", 5)
        self.assertEqual([b.timestamp.astimezone(NY).strftime("%H:%M") for b in got], ["09:30", "15:55"])
        row = _snapshot_row({"ticker": "aaa", "todaysChangePerc": 3.5, "day": {"c": 0, "v": 0},
                             "min": {"c": 12.3, "av": 50000}, "lastTrade": {"p": 12.4}})
        self.assertEqual(row, {"symbol": "AAA", "price": 12.4, "volume": 50000, "change_pct": 3.5})

    def test_snaptrade_quotes_and_order_status(self):
        client = mock.Mock()
        client.trading.get_user_account_quotes.return_value = mock.Mock(
            body=[{"symbol": {"symbol": "AAPL"}, "bid_price": 189.9, "ask_price": 190.1, "last_trade_price": 190}]
        )
        client.account_information.get_user_account_order_detail.return_value = mock.Mock(
            body={"status": "executed", "filled_quantity": "5", "execution_price": "190.05"}
        )
        svc = SnapTradeService(client=client)
        self.assertEqual(svc.get_quotes("u", "s", "a", ["aapl"])["AAPL"]["ask"], 190.1)
        self.assertEqual(svc.get_order_status("u", "s", "a", "o1"),
                         {"status": "EXECUTED", "filled_qty": 5.0, "price": 190.05,
                          "raw": {"status": "executed", "filled_quantity": "5", "execution_price": "190.05"}})


class BackfillTests(TestCase):
    def test_backfill_skips_duplicates(self):
        candles = [
            Candle("AAPL", START + timedelta(days=i), Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), 10)
            for i in range(5)
        ]
        client = mock.Mock(get_aggregates=mock.Mock(return_value=candles))
        first = fetch_historical_data("aapl", "2025-01-01", "2025-01-05", client=client)
        second = fetch_historical_data("AAPL", "2025-01-01", "2025-01-05", client=client)
        self.assertEqual((first["created"], first["skipped"]), (5, 0))
        self.assertEqual((second["created"], second["skipped"]), (0, 5))
        self.assertEqual(StockData.objects.filter(symbol="AAPL").count(), 5)

    @mock.patch("services.massive_service.time.sleep")
    def test_client_retries_rate_limit_and_paginates(self, _sleep):
        def resp(status, payload=None, headers=None):
            r = mock.Mock(status_code=status, headers=headers or {}, text="")
            r.json.return_value = payload or {}
            return r

        bar = {"t": 1735689600000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}
        session = mock.Mock()
        session.get.side_effect = [
            resp(429, headers={"Retry-After": "1"}),
            resp(200, {"status": "OK", "results": [bar], "next_url": "https://x/next"}),
            resp(200, {"status": "OK", "results": [dict(bar, t=1735776000000)]}),
        ]
        client = MassiveClient(api_key="k", base_url="https://x", session=session)
        candles = client.get_aggregates("AAPL", "2025-01-01", "2025-01-02")
        self.assertEqual(len(candles), 2)
        self.assertEqual(session.get.call_count, 3)

    def test_client_raises_on_error(self):
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=403, headers={}, text="forbidden")
        with self.assertRaises(MassiveAPIError):
            MassiveClient(api_key="k", base_url="https://x", session=session).get_aggregates("A", "2025-01-01", "2025-01-02")


class SnapTradeServiceTests(TestCase):
    def _exc(self, status):
        from snaptrade_client.exceptions import ApiException

        exc = ApiException(status=status, reason="err")
        exc.body = {"detail": "nope"}
        exc.headers = {}
        return exc

    @override_settings(TRADING_ENABLED=False)
    def test_order_blocked_when_trading_disabled(self):
        svc = SnapTradeService(client=mock.Mock())
        with self.assertRaises(TradingDisabledError):
            svc.execute_order("u", "s", "a", "AAPL", "BUY", 1)

    @override_settings(TRADING_ENABLED=True, LIVE_TRADING=False)
    def test_live_account_blocked_without_live_flag(self):
        svc = SnapTradeService(client=mock.Mock())
        with self.assertRaises(TradingDisabledError):
            svc.execute_order("u", "s", "a", "AAPL", "BUY", 1, is_paper=False)

    @override_settings(TRADING_ENABLED=True)
    @mock.patch("services.snaptrade_service.time.sleep")
    def test_order_retries_429_then_succeeds(self, _sleep):
        client = mock.Mock()
        client.trading.place_force_order.side_effect = [
            self._exc(429),
            mock.Mock(body={"brokerage_order_id": "B1", "status": "PENDING", "execution_price": None}),
        ]
        result = SnapTradeService(client=client).execute_order("u", "s", "a", "aapl", "buy", 1, is_paper=True)
        self.assertEqual(result["order_id"], "B1")
        kwargs = client.trading.place_force_order.call_args.kwargs
        self.assertEqual((kwargs["symbol"], kwargs["action"]), ("AAPL", "BUY"))

    @override_settings(TRADING_ENABLED=True)
    def test_order_does_not_retry_server_error(self):
        client = mock.Mock()
        client.trading.place_force_order.side_effect = self._exc(500)
        with self.assertRaises(SnapTradeServiceError):
            SnapTradeService(client=client).execute_order("u", "s", "a", "AAPL", "SELL", 1, is_paper=True)
        self.assertEqual(client.trading.place_force_order.call_count, 1)

    def test_parse_accounts_and_positions(self):
        client = mock.Mock()
        client.account_information.list_all_user_accounts.return_value = mock.Mock(
            body={"results": [{"id": "A1", "display_name": "Alpaca Paper", "is_paper": True,
                               "net_value": {"amount": 1000.5, "currency": "USD"}}]}
        )
        client.account_information.get_all_account_positions.return_value = mock.Mock(
            body={"results": [
                {"instrument": {"symbol": "AAPL"}, "units": "3", "price": "200.0", "cash_equivalent": False},
                {"instrument": {"symbol": "USD"}, "units": "50", "price": "1", "cash_equivalent": True},
            ]}
        )
        svc = SnapTradeService(client=client)
        accounts = svc.list_accounts("u", "s")
        self.assertEqual((accounts[0]["id"], accounts[0]["net_value"]), ("A1", 1000.5))
        positions = svc.get_positions("u", "s", "A1")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["market_value"], 600.0)
        self.assertEqual(svc.get_position_units("u", "s", "A1", "aapl"), 3.0)


@override_settings(SNAPTRADE_CLIENT_ID="", SNAPTRADE_CONSUMER_KEY="")


class BackfillRefreshTests(TestCase):
    def test_latest_existing_candle_is_refreshed(self):
        ts = START
        client = mock.Mock(get_aggregates=mock.Mock(return_value=[
            Candle("AAPL", ts, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1.5"), 10)
        ]))
        fetch_historical_data("AAPL", "2025-01-01", "2025-01-01", client=client)
        client.get_aggregates.return_value = [
            Candle("AAPL", ts, Decimal("1"), Decimal("3"), Decimal("1"), Decimal("2.5"), 99)
        ]
        fetch_historical_data("AAPL", "2025-01-01", "2025-01-01", client=client)
        row = StockData.objects.get()
        self.assertEqual((row.close_price, row.volume), (Decimal("2.5"), 99))


