from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal
from io import StringIO
from unittest import mock

import pandas as pd
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from services import momentum_strategy as ms
from services.massive_service import Candle, MassiveAPIError, MassiveClient, fetch_historical_data
from services.snaptrade_service import SnapTradeService, SnapTradeServiceError, TradingDisabledError

from .models import BrokerageAccount, StockData, StrategyConfig, TradeLog

START = datetime(2025, 1, 1, tzinfo=dt_tz.utc)


def make_candles(symbol, closes):
    StockData.objects.bulk_create(
        StockData(
            symbol=symbol,
            timestamp=START + timedelta(days=i),
            open_price=c, high_price=c, low_price=c, close_price=c, volume=1000,
        )
        for i, c in enumerate(closes)
    )


# Falling prices, then a sharp rally on the final bar -> short MA crosses above long MA.
CROSS_UP = [100 - i for i in range(30)] + [200]
CROSS_DOWN = [50 + i for i in range(30)] + [1]


class FakeService:
    def __init__(self, held=0.0, fail=False):
        self.held = held
        self.fail = fail
        self.orders = []

    def get_position_units(self, *args):
        return self.held

    def execute_order(self, **kwargs):
        if self.fail:
            raise SnapTradeServiceError("Insufficient buying power", status=400)
        self.orders.append(kwargs)
        return {"order_id": "ord-1", "status": "EXECUTED", "price": 201.5, "raw": {}}


class IndicatorTests(TestCase):
    def test_sma_crossover_signals(self):
        self.assertEqual(ms.generate_signal(pd.Series(CROSS_UP, dtype=float), 3, 10), ms.BUY)
        self.assertEqual(ms.generate_signal(pd.Series(CROSS_DOWN, dtype=float), 3, 10), ms.SELL)
        self.assertEqual(ms.generate_signal(pd.Series([10.0] * 30), 3, 10), ms.HOLD)

    def test_ema_and_insufficient_data(self):
        self.assertEqual(ms.generate_signal(pd.Series(CROSS_UP, dtype=float), 3, 10, "EMA"), ms.BUY)
        self.assertEqual(ms.generate_signal(pd.Series([1.0, 2.0]), 3, 10), ms.HOLD)


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


class StrategyRunTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("trader", password="pw-123456")
        self.account = BrokerageAccount.objects.create(
            user=self.user, snaptrade_user_id="u1", snaptrade_user_secret="s1",
            account_id="acc-1", account_name="Paper", is_paper=True,
        )
        self.strategy = StrategyConfig.objects.create(
            name="AAPL momentum", symbol="AAPL", is_active=True, short_window=3, long_window=10,
            account=self.account, trade_quantity=Decimal("2"),
        )

    @override_settings(TRADING_ENABLED=False)
    def test_dry_run_logs_skipped_trade(self):
        make_candles("AAPL", CROSS_UP)
        result = ms.MomentumStrategy(self.strategy, snaptrade_service=FakeService()).run()
        self.assertEqual(result.signal, ms.BUY)
        log = TradeLog.objects.get()
        self.assertEqual(log.status, TradeLog.Status.SKIPPED)
        self.strategy.refresh_from_db()
        self.assertEqual(self.strategy.last_signal, ms.BUY)

    @override_settings(TRADING_ENABLED=True)
    def test_buy_executes_once_per_candle(self):
        make_candles("AAPL", CROSS_UP)
        service = FakeService()
        ms.MomentumStrategy(self.strategy, snaptrade_service=service).run()
        ms.MomentumStrategy(self.strategy, snaptrade_service=service).run()
        self.assertEqual(len(service.orders), 1)
        self.assertEqual(service.orders[0]["side"], "BUY")
        log = TradeLog.objects.get()
        self.assertEqual((log.status, log.order_id), (TradeLog.Status.EXECUTED, "ord-1"))

    @override_settings(TRADING_ENABLED=True)
    def test_sell_without_position_is_skipped_and_partial_sell_capped(self):
        make_candles("AAPL", CROSS_DOWN)
        ms.MomentumStrategy(self.strategy, snaptrade_service=FakeService(held=0)).run()
        self.assertEqual(TradeLog.objects.get().status, TradeLog.Status.SKIPPED)

        TradeLog.objects.all().delete()
        self.strategy.last_signal_bar = None
        service = FakeService(held=1.0)
        ms.MomentumStrategy(self.strategy, snaptrade_service=service).run()
        self.assertEqual(float(service.orders[0]["units"]), 1.0)

    @override_settings(TRADING_ENABLED=True)
    def test_broker_error_marks_failed(self):
        make_candles("AAPL", CROSS_UP)
        ms.MomentumStrategy(self.strategy, snaptrade_service=FakeService(fail=True)).run()
        log = TradeLog.objects.get()
        self.assertEqual(log.status, TradeLog.Status.FAILED)
        self.assertIn("Insufficient", log.message)

    def test_not_enough_data(self):
        make_candles("AAPL", [1, 2, 3])
        result = ms.run_strategy(self.strategy)
        self.assertEqual(result.signal, ms.HOLD)
        self.assertIn("Not enough data", result.message)

    @override_settings(TRADING_ENABLED=False)
    def test_management_command(self):
        make_candles("AAPL", CROSS_UP)
        out = StringIO()
        call_command("run_strategy", "--symbol", "AAPL", stdout=out)
        self.assertIn("BUY", out.getvalue())


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
class ViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("trader", password="pw-123456")
        self.strategy = StrategyConfig.objects.create(name="S", symbol="MSFT", short_window=3, long_window=10)
        make_candles("MSFT", CROSS_UP)

    def test_login_required(self):
        response = self.client.get(reverse("trading:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_pages_render(self):
        self.client.force_login(self.user)
        for name in ("trading:dashboard", "trading:positions", "trading:strategies"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("trading:strategy_edit", args=[self.strategy.pk])).status_code, 200)

    def test_chart_json(self):
        self.client.force_login(self.user)
        data = self.client.get(reverse("trading:strategy_chart", args=[self.strategy.pk])).json()
        self.assertEqual(len(data["labels"]), len(CROSS_UP))
        self.assertEqual(data["signals"][-1]["signal"], "BUY")

    def test_toggle_and_create_strategy(self):
        self.client.force_login(self.user)
        self.client.post(reverse("trading:strategy_toggle", args=[self.strategy.pk]))
        self.strategy.refresh_from_db()
        self.assertTrue(self.strategy.is_active)

        response = self.client.post(reverse("trading:strategies"), {
            "name": "Bad", "symbol": "tsla", "ma_type": "SMA", "short_window": 50, "long_window": 10,
            "trade_quantity": "1",
        })
        self.assertEqual(response.status_code, 200)  # validation error re-renders
        self.client.post(reverse("trading:strategies"), {
            "name": "Good", "symbol": "tsla", "ma_type": "EMA", "short_window": 5, "long_window": 20,
            "trade_quantity": "1",
        })
        self.assertTrue(StrategyConfig.objects.get(name="Good").is_active)
        self.assertTrue(StrategyConfig.objects.filter(name="Good", symbol="TSLA").exists())

    def test_admin_pages(self):
        admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw-123456")
        self.client.force_login(admin)
        for url in ("admin:trading_stockdata_changelist", "admin:trading_stockdata_backfill",
                    "admin:trading_strategyconfig_changelist", "admin:trading_tradelog_changelist",
                    "admin:trading_brokerageaccount_changelist"):
            self.assertEqual(self.client.get(reverse(url)).status_code, 200, url)


# Steady uptrend (short MA above long MA, no fresh crossover on the last bar) and the mirror.
UPTREND = [100 + i for i in range(40)]
DOWNTREND = [200 - i for i in range(40)]


class AutoStartTests(TestCase):
    """Creating a strategy: backfill -> evaluate -> switch ON -> trend-based entry."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("trader", password="pw-123456")
        self.account = BrokerageAccount.objects.create(
            user=self.user, snaptrade_user_id="u1", snaptrade_user_secret="s1", account_id="acc-1", is_paper=True,
        )
        self.backfill = mock.patch(
            "services.massive_service.fetch_historical_data",
            return_value={"symbol": "AAPL", "fetched": 0, "created": 0, "skipped": 0},
        )
        self.backfill_mock = self.backfill.start()
        self.addCleanup(self.backfill.stop)

    def make(self, closes, **kw):
        make_candles("AAPL", closes)
        return StrategyConfig.objects.create(
            name="A", symbol="AAPL", short_window=3, long_window=10, account=self.account, **kw
        )

    @override_settings(TRADING_ENABLED=True)
    def test_uptrend_buys_and_turns_on(self):
        strategy = self.make(UPTREND)
        service = FakeService()
        result = ms.start_strategy(strategy, snaptrade_service=service)
        self.backfill_mock.assert_called_once()
        strategy.refresh_from_db()
        self.assertTrue(strategy.is_active)
        self.assertEqual((strategy.trend, result.signal), ("UP", "HOLD"))
        self.assertEqual(service.orders[0]["side"], "BUY")
        self.assertEqual(TradeLog.objects.get().status, TradeLog.Status.EXECUTED)
        # The scheduler's next run on the same candle must not buy again.
        ms.MomentumStrategy(strategy, snaptrade_service=service).run()
        self.assertEqual(len(service.orders), 1)

    @override_settings(TRADING_ENABLED=True)
    def test_downtrend_and_sell_do_not_buy(self):
        for closes in (DOWNTREND, CROSS_DOWN):
            StockData.objects.all().delete()
            strategy = self.make(closes)
            service = FakeService()
            ms.start_strategy(strategy, snaptrade_service=service)
            strategy.refresh_from_db()
            self.assertTrue(strategy.is_active)
            self.assertEqual(service.orders, [])
        self.assertFalse(TradeLog.objects.exists())

    @override_settings(TRADING_ENABLED=True)
    def test_entry_skipped_when_already_holding(self):
        strategy = self.make(UPTREND)
        service = FakeService(held=5)
        ms.start_strategy(strategy, snaptrade_service=service)
        self.assertEqual(service.orders, [])
        self.assertEqual(TradeLog.objects.get().status, TradeLog.Status.SKIPPED)

    @override_settings(TRADING_ENABLED=False)
    def test_dry_run_entry_logged_as_skipped(self):
        strategy = self.make(CROSS_UP)
        ms.start_strategy(strategy, snaptrade_service=FakeService())
        self.assertEqual(TradeLog.objects.get().status, TradeLog.Status.SKIPPED)

    def test_no_data_still_turns_on(self):
        strategy = self.make([])
        result = ms.start_strategy(strategy, snaptrade_service=FakeService())
        strategy.refresh_from_db()
        self.assertTrue(strategy.is_active)
        self.assertIn("Not enough data", result.message)

    @override_settings(TRADING_ENABLED=False)
    def test_create_via_web_form_auto_starts(self):
        make_candles("AAPL", UPTREND)
        self.client.force_login(self.user)
        response = self.client.post(reverse("trading:strategies"), {
            "symbol": "aapl", "trade_quantity": "2", "account": self.account.pk,
            "ma_type": "SMA", "short_window": 3, "long_window": 10, "name": "",
        }, follow=True)
        strategy = StrategyConfig.objects.get()
        self.assertEqual(strategy.name, "AAPL SMA 3/10")
        self.assertTrue(strategy.is_active)
        self.assertEqual(strategy.trend, "UP")
        self.assertContains(response, "Dry run")
        self.assertEqual(TradeLog.objects.get().action, "BUY")

    @override_settings(TRADING_ENABLED=False)
    def test_scheduler_once_runs_active_strategies(self):
        self.make(CROSS_UP, is_active=True)
        StrategyConfig.objects.create(name="off", symbol="MSFT", short_window=3, long_window=10)
        out = StringIO()
        call_command("run_scheduler", "--once", stdout=out)
        self.assertIn("AAPL: BUY", out.getvalue())
        self.assertNotIn("MSFT", out.getvalue())

    def test_market_hours(self):
        from zoneinfo import ZoneInfo

        from trading.management.commands.run_scheduler import market_is_open

        ny = ZoneInfo("America/New_York")
        self.assertTrue(market_is_open(datetime(2026, 11, 2, 10, 0, tzinfo=ny)))   # Monday
        self.assertFalse(market_is_open(datetime(2026, 11, 2, 16, 30, tzinfo=ny)))
        self.assertFalse(market_is_open(datetime(2026, 11, 7, 11, 0, tzinfo=ny)))  # Saturday


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


# ---------------------------------------------------------------------------
# Thai market (Settrade)
# ---------------------------------------------------------------------------
from services import settrade_service as st  # noqa: E402
from services.errors import OrderSkipped  # noqa: E402


def make_th_candles(symbol, closes):
    StockData.objects.bulk_create(
        StockData(
            market="TH", symbol=symbol, timestamp=START + timedelta(days=i),
            open_price=c, high_price=c, low_price=c, close_price=c, volume=1000,
        )
        for i, c in enumerate(closes)
    )


class FakeEquity:
    def __init__(self, portfolio=None, info=None):
        self.orders = []
        self.portfolio = portfolio if portfolio is not None else {"portfolioList": []}
        self.info = info or {"cashBalance": 50000, "lineAvailable": 40000}

    def get_portfolios(self):
        return self.portfolio

    def get_account_info(self):
        return self.info

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderNo": "TH-1"}


class FakeMarket:
    def __init__(self, last=33.75, candles=None):
        self.last = last
        self.candles = candles or {"time": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

    def get_quote_symbol(self, symbol):
        return {"symbol": symbol, "last": self.last}

    def get_candlestick(self, **kwargs):
        return self.candles


class FakeInvestor:
    def __init__(self, equity=None, market=None):
        self.equity = equity or FakeEquity()
        self.market = market or FakeMarket()

    def Equity(self, account_no):
        return self.equity

    def MarketData(self):
        return self.market


class ThaiRulesTests(TestCase):
    def test_tick_rounding(self):
        self.assertEqual(st.round_to_tick(1.234, "down"), Decimal("1.23"))
        self.assertEqual(st.round_to_tick(33.9, "down"), Decimal("33.75"))
        self.assertEqual(st.round_to_tick(33.9, "up"), Decimal("34.00"))
        self.assertEqual(st.round_to_tick(150.3, "up"), Decimal("150.50"))
        self.assertEqual(st.round_to_tick(401, "down"), Decimal("400.00"))

    def test_board_lots_and_limit_price(self):
        self.assertEqual(st.to_board_lots(250), 200)
        self.assertEqual(st.to_board_lots(99), 0)
        self.assertEqual(st.limit_price(34, "BUY", 1), Decimal("34.25"))   # 34.34 -> down to 0.25 step
        self.assertEqual(st.limit_price(34, "SELL", 1), Decimal("33.75"))  # 33.66 -> up to 0.25 step
        self.assertEqual(st.limit_price(34, "BUY", 0), Decimal("34.00"))

    def test_parse_candles_both_shapes(self):
        cols = {"time": [1735689600, 1735776000], "open": [1, 2], "high": [1, 2], "low": [1, 2],
                "close": [1.5, 2.5], "volume": [10, 20]}
        candles = st.parse_candles("ptt", cols)
        self.assertEqual([c.close for c in candles], [Decimal("1.5"), Decimal("2.5")])
        rows = [{"time": "2025-01-01T00:00:00", "open": 1, "high": 1, "low": 1, "close": 3, "volume": 5}]
        self.assertEqual(st.parse_candles("PTT", rows)[0].close, Decimal("3"))

    def test_parse_positions(self):
        payload = {"portfolioList": [
            {"symbol": "PTT", "actualVolume": 300, "availableVolume": 200, "marketPrice": 34, "averagePrice": 32},
            {"symbol": "AOT", "actualVolume": 0},
        ]}
        positions = st.parse_positions(payload)
        self.assertEqual(len(positions), 1)
        self.assertEqual((positions[0]["units"], positions[0]["available_units"]), (300, 200))
        self.assertEqual(positions[0]["market_value"], 10200)


class SettradeServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("thai", password="pw-123456")
        self.account = BrokerageAccount.objects.create(
            user=self.user, provider="SETTRADE", account_id="1234567", settrade_broker_id="SANDBOX",
            settrade_app_id="app", settrade_app_secret="secret", settrade_app_code="code", settrade_pin="000000",
        )

    def service(self, investor):
        return st.SettradeService(self.account, investor_factory=lambda acc: investor)

    def test_credentials_encrypted_and_sandbox_is_paper(self):
        from django.db import connection

        self.account.refresh_from_db()
        self.assertTrue(self.account.is_paper)
        self.assertEqual(self.account.settrade_pin, "000000")
        with connection.cursor() as cur:
            cur.execute("SELECT settrade_pin, settrade_app_secret FROM trading_brokerageaccount WHERE id=%s", [self.account.pk])
            raw_pin, raw_secret = cur.fetchone()
        self.assertTrue(raw_pin.startswith("enc1:") and raw_secret.startswith("enc1:"))
        self.assertNotIn("000000", raw_pin)

    @override_settings(TRADING_ENABLED=True)
    def test_places_limit_order_in_lots(self):
        investor = FakeInvestor()
        order = self.service(investor).execute_order("ptt", "BUY", 250, slippage_pct=1)
        placed = investor.equity.orders[0]
        self.assertEqual((placed["side"], placed["symbol"], placed["volume"]), ("Buy", "PTT", 200))
        self.assertEqual((placed["price"], placed["price_type"], placed["pin"]), (34.0, "Limit", "000000"))
        self.assertEqual((order["order_id"], order["quantity"]), ("TH-1", 200))

    @override_settings(TRADING_ENABLED=True)
    def test_below_one_lot_is_skipped(self):
        with self.assertRaises(OrderSkipped):
            self.service(FakeInvestor()).execute_order("PTT", "BUY", 50)

    @override_settings(TRADING_ENABLED=True, LIVE_TRADING=False)
    def test_live_settrade_account_blocked(self):
        self.account.settrade_broker_id = "023"
        self.account.save()
        self.assertFalse(self.account.is_paper)
        with self.assertRaises(TradingDisabledError):
            self.service(FakeInvestor()).execute_order("PTT", "BUY", 100)

    @override_settings(TRADING_ENABLED=False)
    def test_dry_run_blocked(self):
        with self.assertRaises(TradingDisabledError):
            self.service(FakeInvestor()).execute_order("PTT", "BUY", 100)

    def test_balances_and_sellable_units(self):
        investor = FakeInvestor(FakeEquity({"portfolioList": [
            {"symbol": "PTT", "actualVolume": 300, "availableVolume": 200, "marketPrice": 34}]}))
        svc = self.service(investor)
        self.assertEqual(svc.get_position_units("ptt"), 200)
        balances = svc.get_balances()
        self.assertEqual((balances["cash"], balances["net_value"], balances["currency"]), (50000, 50000 + 10200, "THB"))

    def test_sdk_environment_is_reset_before_login(self):
        from settrade_v2.config import config as sdk_config

        with mock.patch("settrade_v2.Investor") as investor_cls:
            st.SettradeService(self.account)._login()
            self.assertEqual(sdk_config["environment"], "uat")
            self.account.settrade_broker_id = "023"
            self.account.save()
            st.SettradeService(self.account)._login()
            self.assertEqual(sdk_config["environment"], "prod")
            self.assertEqual(investor_cls.call_args.kwargs["broker_id"], "023")

    def test_sdk_errors_become_broker_errors(self):
        from settrade_v2.errors import SettradeError

        class BadEquity(FakeEquity):
            def get_portfolios(self):
                raise SettradeError(code="E1", message="Invalid account", status_code=400)

        with self.assertRaises(st.SettradeServiceError):
            self.service(FakeInvestor(BadEquity())).get_positions()


class ThaiStrategyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("thai", password="pw-123456")
        self.account = BrokerageAccount.objects.create(
            user=self.user, provider="SETTRADE", account_id="1234567", settrade_broker_id="SANDBOX",
            settrade_app_id="app", settrade_app_secret="secret", settrade_app_code="code", settrade_pin="000000",
        )

    @override_settings(TRADING_ENABLED=True)
    def test_th_strategy_backfills_from_settrade_and_buys_lots(self):
        closes = UPTREND
        candles = {"time": [int((START + timedelta(days=i)).timestamp()) for i in range(len(closes))],
                   "open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(closes)}
        investor = FakeInvestor(market=FakeMarket(last=139, candles=candles))
        strategy = StrategyConfig.objects.create(
            name="PTT", market="TH", symbol="PTT", short_window=3, long_window=10,
            account=self.account, trade_quantity=Decimal("150"),
        )
        with mock.patch.object(st.SettradeService, "_login", lambda self: investor):
            result = ms.start_strategy(strategy)
        self.assertEqual(StockData.objects.filter(market="TH", symbol="PTT").count(), len(closes))
        self.assertFalse(StockData.objects.filter(market="US").exists())
        self.assertEqual(result.extra["trend"], "UP")
        self.assertEqual(investor.equity.orders[0]["volume"], 100)
        log = TradeLog.objects.get()
        self.assertEqual((log.status, log.quantity, log.order_id), ("EXECUTED", Decimal("100"), "TH-1"))

    def test_markets_are_isolated(self):
        make_candles("PTT", CROSS_UP)            # a US ticker that happens to share the name
        make_th_candles("PTT", [30.0] * 40)      # flat Thai prices
        th = StrategyConfig.objects.create(name="t", market="TH", symbol="PTT", short_window=3, long_window=10)
        us = StrategyConfig.objects.create(name="u", market="US", symbol="PTT", short_window=3, long_window=10)
        self.assertEqual(ms.MomentumStrategy(th).evaluate().signal, "HOLD")
        self.assertEqual(ms.MomentumStrategy(us).evaluate().signal, "BUY")

    @override_settings(TRADING_ENABLED=True)
    def test_account_from_wrong_market_does_not_trade(self):
        make_candles("AAPL", CROSS_UP)
        strategy = StrategyConfig.objects.create(
            name="x", market="US", symbol="AAPL", short_window=3, long_window=10, account=self.account
        )
        result = ms.MomentumStrategy(strategy, broker=mock.Mock()).run()
        self.assertIn("TH market", result.message)
        self.assertFalse(TradeLog.objects.exists())

    def test_thai_hours(self):
        from zoneinfo import ZoneInfo

        from trading.management.commands.run_scheduler import market_is_open

        bkk = ZoneInfo("Asia/Bangkok")
        self.assertTrue(market_is_open(datetime(2026, 11, 2, 10, 30, tzinfo=bkk), "TH"))
        self.assertFalse(market_is_open(datetime(2026, 11, 2, 13, 0, tzinfo=bkk), "TH"))  # lunch break
        self.assertTrue(market_is_open(datetime(2026, 11, 2, 15, 0, tzinfo=bkk), "TH"))
        self.assertFalse(market_is_open(datetime(2026, 11, 2, 15, 0, tzinfo=bkk), "US"))


@override_settings(SNAPTRADE_CLIENT_ID="", SNAPTRADE_CONSUMER_KEY="")
class ThaiUITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("thai", password="pw-123456")
        self.client.force_login(self.user)
        self.client.post(reverse("trading:set_market", args=["TH"]))

    def add_account(self, **extra):
        data = {"account_name": "", "account_id": "1234567", "settrade_broker_id": "sandbox",
                "settrade_app_id": "app", "settrade_app_secret": "s", "settrade_app_code": "c", "settrade_pin": "111111"}
        data.update(extra)
        return self.client.post(reverse("trading:settings"), data)

    def test_switch_and_pages_render_in_th(self):
        self.assertEqual(self.client.session["market"], "TH")
        for name in ("trading:dashboard", "trading:positions", "trading:strategies", "trading:settings"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
        self.assertContains(self.client.get(reverse("trading:settings")), "Add Settrade account")
        self.assertContains(self.client.get(reverse("trading:dashboard")), "฿")

    def test_add_and_edit_settrade_account_keeps_secrets(self):
        self.add_account()
        account = BrokerageAccount.objects.get()
        self.assertEqual((account.provider, account.settrade_broker_id, account.is_paper), ("SETTRADE", "SANDBOX", True))
        self.assertEqual(account.account_name, "Settrade 1234567")
        self.client.post(reverse("trading:settrade_edit", args=[account.pk]), {
            "account_name": "Main", "account_id": "1234567", "settrade_broker_id": "SANDBOX",
            "settrade_app_id": "app", "settrade_app_secret": "", "settrade_app_code": "", "settrade_pin": "",
        })
        account.refresh_from_db()
        self.assertEqual((account.account_name, account.settrade_pin, account.settrade_app_secret), ("Main", "111111", "s"))

    def test_th_strategy_form_rules_and_filtering(self):
        self.add_account()
        StrategyConfig.objects.create(name="us", market="US", symbol="SPY", short_window=3, long_window=10)
        response = self.client.post(reverse("trading:strategies"), {
            "symbol": "ptt", "trade_quantity": "50", "ma_type": "SMA", "short_window": 10, "long_window": 50,
        })
        self.assertContains(response, "lots of 100")
        with mock.patch("trading.views.start_strategy") as start:
            start.return_value = ms.StrategyResult(strategy="x", symbol="PTT", signal="HOLD", message="ok")
            self.client.post(reverse("trading:strategies"), {
                "symbol": "ptt", "trade_quantity": "200", "ma_type": "SMA", "short_window": 10,
                "long_window": 50, "slippage_pct": "0.5", "account": BrokerageAccount.objects.get().pk,
            })
        strategy = StrategyConfig.objects.get(symbol="PTT")
        self.assertEqual((strategy.market, strategy.slippage_pct), ("TH", Decimal("0.50")))
        self.assertEqual(strategy.account.provider, "SETTRADE")
        page = self.client.get(reverse("trading:strategies")).content.decode()
        self.assertIn("PTT", page)
        self.assertNotIn("SPY", page.split("<tbody>")[1])

    def test_connection_button(self):
        self.add_account()
        account = BrokerageAccount.objects.get()
        with mock.patch.object(st.SettradeService, "test_connection", return_value={"ok": True, "steps": ["Account info OK"]}):
            response = self.client.post(reverse("trading:settrade_test", args=[account.pk]), follow=True)
        self.assertContains(response, "Account info OK")
