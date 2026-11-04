from datetime import date, timedelta

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path, reverse

from services.massive_service import MassiveAPIError, fetch_historical_data

from .models import BrokerageAccount, DayTradeSettings, ObserveItem, Position, ScanResult, StockData, TradeLog


class BackfillForm(forms.Form):
    symbol = forms.CharField(max_length=16, help_text="Ticker, e.g. AAPL")
    start_date = forms.DateField(initial=lambda: date.today() - timedelta(days=365))
    end_date = forms.DateField(initial=date.today)
    timespan = forms.ChoiceField(
        choices=[(c, c) for c in ("day", "hour", "minute", "week", "month")], initial="day"
    )
    multiplier = forms.IntegerField(min_value=1, initial=1)


@admin.register(BrokerageAccount)
class BrokerageAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "brokerage_name", "account_name", "account_id", "is_paper", "created_at")
    list_filter = ("brokerage_name", "is_paper")
    search_fields = ("user__username", "account_id", "account_name", "snaptrade_user_id")
    readonly_fields = ("created_at",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields["snaptrade_user_secret"].widget = forms.PasswordInput(render_value=True)
        return form


@admin.register(StockData)
class StockDataAdmin(admin.ModelAdmin):
    list_display = ("symbol", "timestamp", "open_price", "high_price", "low_price", "close_price", "volume")
    list_filter = ("symbol",)
    search_fields = ("symbol",)
    date_hierarchy = "timestamp"
    ordering = ("symbol", "-timestamp")
    change_list_template = "admin/trading/stockdata/change_list.html"

    def get_urls(self):
        custom = [path("backfill/", self.admin_site.admin_view(self.backfill_view), name="trading_stockdata_backfill")]
        return custom + super().get_urls()

    def backfill_view(self, request):
        form = BackfillForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            d = form.cleaned_data
            try:
                s = fetch_historical_data(
                    d["symbol"], d["start_date"], d["end_date"], timespan=d["timespan"], multiplier=d["multiplier"]
                )
                self.message_user(request, f"{s['symbol']}: fetched {s['fetched']}, created {s['created']}.")
                return redirect(reverse("admin:trading_stockdata_changelist") + f"?symbol={s['symbol']}")
            except MassiveAPIError as exc:
                self.message_user(request, f"Backfill failed: {exc}", messages.ERROR)
        context = {**self.admin_site.each_context(request), "opts": self.model._meta, "form": form,
                   "title": "Backfill historical data"}
        return render(request, "admin/trading/stockdata/backfill.html", context)


@admin.register(DayTradeSettings)
class DayTradeSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "auto_trading", "account", "timeframe_minutes", "block_usd", "stop_loss_pct", "liquidate_at")
    readonly_fields = ("last_scan_at", "last_cycle_at", "last_liquidation_date")

    def has_add_permission(self, request):
        return not DayTradeSettings.objects.exists()


class OrderInline(admin.TabularInline):
    model = TradeLog
    extra = 0
    fields = ("created_at", "action", "reason", "order_type", "quantity", "limit_price", "price", "status", "order_id")
    readonly_fields = fields
    can_delete = False


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("symbol", "status", "simulated", "blocks", "quantity", "avg_price", "stop_price",
                    "last_price", "exit_price", "exit_reason", "opened_at", "closed_at")
    list_filter = ("status", "simulated", "exit_reason")
    search_fields = ("symbol",)
    inlines = [OrderInline]


@admin.register(ObserveItem)
class ObserveItemAdmin(admin.ModelAdmin):
    list_display = ("symbol", "reason", "added_at", "note")
    list_filter = ("reason",)


@admin.register(ScanResult)
class ScanResultAdmin(admin.ModelAdmin):
    list_display = ("scanned_at", "symbol", "outcome", "price", "change_pct", "volume", "daily_sma50", "note")
    list_filter = ("outcome",)
    search_fields = ("symbol",)
    date_hierarchy = "scanned_at"


@admin.register(TradeLog)
class TradeLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "symbol", "action", "reason", "order_type", "quantity", "limit_price",
                    "price", "status", "order_id")
    list_filter = ("status", "action", "reason")
    search_fields = ("symbol", "order_id", "message")
    readonly_fields = [f.name for f in TradeLog._meta.fields]

    def has_add_permission(self, request):
        return False
