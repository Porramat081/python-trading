from datetime import time

from django.conf import settings
from django.db import models
from django.utils import timezone


class BrokerageAccount(models.Model):
    """A brokerage account (Alpaca) linked to a Django user through SnapTrade."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="brokerage_accounts"
    )
    snaptrade_user_id = models.CharField(max_length=255)
    snaptrade_user_secret = models.CharField(max_length=255)
    account_id = models.CharField(
        max_length=255, blank=True, help_text="SnapTrade account ID of the linked Alpaca account."
    )
    account_name = models.CharField(max_length=255, blank=True)
    brokerage_name = models.CharField(max_length=100, default="Alpaca")
    is_paper = models.BooleanField(
        default=True, help_text="Paper (simulated) account. Live accounts need LIVE_TRADING=True."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["snaptrade_user_id", "account_id"], name="unique_snaptrade_account"
            )
        ]

    def __str__(self):
        label = self.account_name or self.account_id or "not linked"
        return f"{self.brokerage_name} ({label}) - {self.user}"

    @property
    def is_linked(self) -> bool:
        return bool(self.account_id)


class StockData(models.Model):
    """One OHLCV candle (historical / backfilled price data)."""

    symbol = models.CharField(max_length=16, db_index=True)
    timestamp = models.DateTimeField()
    open_price = models.DecimalField(max_digits=16, decimal_places=4)
    high_price = models.DecimalField(max_digits=16, decimal_places=4)
    low_price = models.DecimalField(max_digits=16, decimal_places=4)
    close_price = models.DecimalField(max_digits=16, decimal_places=4)
    volume = models.BigIntegerField()

    class Meta:
        ordering = ["symbol", "timestamp"]
        verbose_name_plural = "Stock data"
        constraints = [
            models.UniqueConstraint(fields=["symbol", "timestamp"], name="unique_symbol_timestamp")
        ]
        indexes = [models.Index(fields=["symbol", "-timestamp"])]

    def __str__(self):
        return f"{self.symbol} {self.timestamp:%Y-%m-%d %H:%M} close={self.close_price}"


class DayTradeSettings(models.Model):
    """Single row holding the day-trading configuration (see the strategy spec)."""

    class Source(models.TextChoices):
        GAINERS = "GAINERS", "Top gainers"
        MOST_ACTIVE = "MOST_ACTIVE", "Most active (volume)"
        WATCHLIST = "WATCHLIST", "My watchlist only"

    class Provider(models.TextChoices):
        ALPACA = "ALPACA", "Alpaca (free, real-time IEX)"
        MASSIVE = "MASSIVE", "Massive (paid plan)"

    # Market data
    data_provider = models.CharField(max_length=8, choices=Provider.choices, default=Provider.ALPACA)
    watchlist = models.TextField(
        blank=True, help_text="Tickers separated by commas/spaces. Used as the screener when chosen, "
        "or as a fallback when the screener fails or returns nothing.",
    )

    # Automation
    auto_trading = models.BooleanField(default=False, help_text="Run the day-trading loop on schedule.")
    account = models.ForeignKey(
        BrokerageAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Alpaca account that receives orders.",
    )
    loop_minutes = models.PositiveIntegerField(default=2, help_text="Minutes between monitoring loops (1-5).")

    # Execution timeframe and risk
    timeframe_minutes = models.PositiveIntegerField(
        default=5, choices=[(5, "5-minute"), (15, "15-minute")], help_text="Execution chart timeframe."
    )
    block_usd = models.DecimalField(max_digits=12, decimal_places=2, default=1000, help_text="$ per entry block.")
    max_blocks = models.PositiveIntegerField(default=2, help_text="Max entry blocks per ticker (pyramiding cap).")
    max_open_positions = models.PositiveIntegerField(default=5, help_text="Max tickers held at once.")
    stop_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=1.5, help_text="Hard stop-loss %.")
    limit_offset = models.DecimalField(
        max_digits=6, decimal_places=2, default=0.02, help_text="Limit buy = ask + this many dollars."
    )
    buy_timeout_minutes = models.PositiveIntegerField(
        default=5, help_text="Cancel an unfilled limit buy after this many minutes."
    )
    liquidate_at = models.TimeField(default=time(15, 45), help_text="Sell everything at this time (ET).")
    observe_minutes = models.PositiveIntegerField(default=60, help_text="Unowned observe-list entries expire after this.")

    # Screener (New Stock List)
    screener_source = models.CharField(max_length=12, choices=Source.choices, default=Source.GAINERS)
    screener_count = models.PositiveIntegerField(default=10, help_text="Candidates to evaluate per scan.")
    min_price = models.DecimalField(max_digits=10, decimal_places=2, default=5)
    max_price = models.DecimalField(max_digits=10, decimal_places=2, default=500)
    min_volume = models.PositiveBigIntegerField(default=1_000_000, help_text="Minimum shares traded today.")
    last_scan_at = models.DateTimeField(null=True, blank=True)
    last_cycle_at = models.DateTimeField(null=True, blank=True)
    last_liquidation_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = "Day-trading settings"

    def __str__(self):
        return "Day-trading settings"

    @classmethod
    def load(cls) -> "DayTradeSettings":
        obj, created = cls.objects.get_or_create(pk=1)
        if created:
            obj.refresh_from_db()  # turn Python defaults into proper Decimal/time values
        return obj


class Position(models.Model):
    """An intraday position opened by the bot (the spec's Current Holding List)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Entry pending"
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"
        CANCELED = "CANCELED", "Entry not filled"

    account = models.ForeignKey(BrokerageAccount, on_delete=models.SET_NULL, null=True, blank=True)
    symbol = models.CharField(max_length=16, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    simulated = models.BooleanField(default=False, help_text="Dry run: fills simulated, no broker orders.")
    blocks = models.PositiveIntegerField(default=0)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    avg_price = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    stop_price = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    last_price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    exit_price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    exit_reason = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"{self.symbol} {self.quantity} @ {self.avg_price} [{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.OPEN)

    @property
    def pnl(self) -> float | None:
        price = self.exit_price if self.status == self.Status.CLOSED else self.last_price
        if price is None or not self.quantity:
            return None
        return float((price - self.avg_price) * self.quantity)

    @property
    def pnl_pct(self) -> float | None:
        price = self.exit_price if self.status == self.Status.CLOSED else self.last_price
        if price is None or not self.avg_price:
            return None
        return float((price / self.avg_price - 1) * 100)


class ObserveItem(models.Model):
    """The spec's Observe List: developing setups and flagged holdings."""

    class Reason(models.TextChoices):
        DEVELOPING = "DEVELOPING", "Developing setup"
        WARNING = "WARNING", "Momentum warning (owned)"

    symbol = models.CharField(max_length=16, unique=True)
    reason = models.CharField(max_length=12, choices=Reason.choices, default=Reason.DEVELOPING)
    added_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["added_at"]

    def __str__(self):
        return f"{self.symbol} ({self.reason})"


class ScanResult(models.Model):
    """Outcome of the morning screener for each candidate (Case 1)."""

    class Outcome(models.TextChoices):
        BUY = "BUY", "Buy order"
        OBSERVE = "OBSERVE", "Observe list"
        DISCARD = "DISCARD", "Discarded"
        BELOW_SMA = "BELOW_SMA", "Below daily 50 SMA"
        HELD = "HELD", "Already held"
        SKIPPED = "SKIPPED", "Skipped"

    scanned_at = models.DateTimeField(default=timezone.now, db_index=True)
    symbol = models.CharField(max_length=16)
    price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    change_pct = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    volume = models.PositiveBigIntegerField(null=True, blank=True)
    daily_sma50 = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-scanned_at", "symbol"]

    def __str__(self):
        return f"{self.symbol}: {self.outcome}"


class TradeLog(models.Model):
    """Record of every order the system attempted."""

    class Action(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        EXECUTED = "EXECUTED", "Executed"
        SIMULATED = "SIMULATED", "Simulated (dry run)"
        CANCELED = "CANCELED", "Canceled (not filled)"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    class Reason(models.TextChoices):
        ENTRY = "ENTRY", "Entry"
        ADD_ON = "ADD_ON", "Add-on block"
        EXIT_SIGNAL = "EXIT_SIGNAL", "Exit signal"
        STOP_LOSS = "STOP_LOSS", "Stop-loss"
        EOD = "EOD", "End-of-day liquidation"
        MANUAL = "MANUAL", "Manual"

    account = models.ForeignKey(
        BrokerageAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name="trade_logs"
    )
    position = models.ForeignKey(
        Position, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )
    symbol = models.CharField(max_length=16)
    action = models.CharField(max_length=4, choices=Action.choices)
    reason = models.CharField(max_length=12, choices=Reason.choices, default=Reason.MANUAL)
    order_type = models.CharField(max_length=10, default="Market")
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    limit_price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, help_text="Fill price.")
    filled_quantity = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    order_id = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.quantity} {self.symbol} [{self.status}]"
