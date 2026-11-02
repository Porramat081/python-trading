from django.conf import settings
from django.db import models

from .crypto import EncryptedTextField


class Market(models.TextChoices):
    US = "US", "US stocks"
    TH = "TH", "Thai stocks (SET)"


MARKET_CURRENCY = {Market.US: "USD", Market.TH: "THB"}
MARKET_SYMBOL = {Market.US: "$", Market.TH: "฿"}


class BrokerageAccount(models.Model):
    """A brokerage account: Alpaca via SnapTrade (US) or a Thai broker via Settrade Open API (TH)."""

    class Provider(models.TextChoices):
        SNAPTRADE = "SNAPTRADE", "Alpaca (SnapTrade)"
        SETTRADE = "SETTRADE", "Settrade Open API"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="brokerage_accounts"
    )
    provider = models.CharField(max_length=10, choices=Provider.choices, default=Provider.SNAPTRADE)
    snaptrade_user_id = models.CharField(max_length=255, blank=True)
    snaptrade_user_secret = models.CharField(max_length=255, blank=True)
    account_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="SnapTrade account ID (Alpaca) or Settrade trading account number.",
    )
    # Settrade Open API credentials (secrets are encrypted at rest).
    settrade_broker_id = models.CharField(
        max_length=20, blank=True, help_text="Your broker's ID, or SANDBOX for the test environment."
    )
    settrade_app_id = EncryptedTextField(blank=True)
    settrade_app_secret = EncryptedTextField(blank=True)
    settrade_app_code = EncryptedTextField(blank=True)
    settrade_pin = EncryptedTextField(blank=True)
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
                fields=["provider", "snaptrade_user_id", "account_id"], name="unique_broker_account"
            )
        ]

    def __str__(self):
        label = self.account_name or self.account_id or "not linked"
        return f"{self.brokerage_name} ({label}) - {self.user}"

    def save(self, *args, **kwargs):
        if self.provider == self.Provider.SETTRADE:
            self.is_paper = self.settrade_broker_id.strip().upper() == "SANDBOX"
            if self.brokerage_name in ("", "Alpaca"):
                self.brokerage_name = "Settrade"
        super().save(*args, **kwargs)

    @property
    def is_settrade(self) -> bool:
        return self.provider == self.Provider.SETTRADE

    @property
    def market(self) -> str:
        return Market.TH if self.is_settrade else Market.US

    @property
    def currency(self) -> str:
        return MARKET_CURRENCY[self.market]

    @property
    def is_linked(self) -> bool:
        if self.is_settrade:
            return bool(
                self.account_id and self.settrade_broker_id and self.settrade_app_id
                and self.settrade_app_secret and self.settrade_app_code
            )
        return bool(self.account_id)


class StockData(models.Model):
    """One OHLCV candle (historical / backfilled price data)."""

    market = models.CharField(max_length=2, choices=Market.choices, default=Market.US)
    symbol = models.CharField(max_length=16, db_index=True)
    timestamp = models.DateTimeField()
    open_price = models.DecimalField(max_digits=16, decimal_places=4)
    high_price = models.DecimalField(max_digits=16, decimal_places=4)
    low_price = models.DecimalField(max_digits=16, decimal_places=4)
    close_price = models.DecimalField(max_digits=16, decimal_places=4)
    volume = models.BigIntegerField()

    class Meta:
        ordering = ["market", "symbol", "timestamp"]
        verbose_name_plural = "Stock data"
        constraints = [
            models.UniqueConstraint(
                fields=["market", "symbol", "timestamp"], name="unique_market_symbol_timestamp"
            )
        ]
        indexes = [models.Index(fields=["market", "symbol", "-timestamp"])]

    def __str__(self):
        return f"{self.market}:{self.symbol} {self.timestamp:%Y-%m-%d %H:%M} close={self.close_price}"


class StrategyConfig(models.Model):
    """Configuration of a moving-average crossover momentum strategy."""

    class MAType(models.TextChoices):
        SMA = "SMA", "Simple moving average"
        EMA = "EMA", "Exponential moving average"

    name = models.CharField(max_length=100)
    market = models.CharField(max_length=2, choices=Market.choices, default=Market.US, db_index=True)
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
        max_digits=12, decimal_places=4, default=1,
        help_text="Shares per order. Thai orders are rounded down to whole lots of 100.",
    )
    slippage_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Thai limit orders: % above last price to buy / below to sell (helps fills).",
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
        if self.account_id and self.account and self.account.market != self.market:
            raise ValidationError({"account": "Account belongs to a different market."})

    @property
    def currency_symbol(self) -> str:
        return MARKET_SYMBOL[self.market]


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
