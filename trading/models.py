from django.conf import settings
from django.db import models


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


class StrategyConfig(models.Model):
    """Configuration of a moving-average crossover momentum strategy."""

    class MAType(models.TextChoices):
        SMA = "SMA", "Simple moving average"
        EMA = "EMA", "Exponential moving average"

    name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=16, db_index=True)
    is_active = models.BooleanField(
        default=False, help_text="Active strategies are run automatically by the scheduler."
    )
    short_window = models.PositiveIntegerField(default=10)
    long_window = models.PositiveIntegerField(default=50)
    ma_type = models.CharField(max_length=3, choices=MAType.choices, default=MAType.SMA)
    account = models.ForeignKey(
        BrokerageAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="strategies",
        help_text="Account that receives orders when a signal fires.",
    )
    trade_quantity = models.DecimalField(
        max_digits=12, decimal_places=4, default=1, help_text="Units per order."
    )
    last_run = models.DateTimeField(null=True, blank=True)
    last_signal = models.CharField(max_length=4, blank=True)
    trend = models.CharField(max_length=4, blank=True, help_text="UP if short MA > long MA, else DOWN.")
    last_signal_bar = models.DateTimeField(
        null=True, blank=True, help_text="Candle time of the last signal acted on (prevents repeats)."
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.symbol} {self.ma_type} {self.short_window}/{self.long_window})"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.short_window and self.long_window and self.short_window >= self.long_window:
            raise ValidationError({"short_window": "Short window must be smaller than long window."})
        if self.symbol:
            self.symbol = self.symbol.strip().upper()


class TradeLog(models.Model):
    """Record of every order the system attempted."""

    class Action(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        EXECUTED = "EXECUTED", "Executed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped (dry run)"

    account = models.ForeignKey(
        BrokerageAccount, on_delete=models.CASCADE, related_name="trade_logs"
    )
    strategy = models.ForeignKey(
        StrategyConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name="trade_logs"
    )
    symbol = models.CharField(max_length=16)
    action = models.CharField(max_length=4, choices=Action.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    order_id = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.quantity} {self.symbol} [{self.status}]"
