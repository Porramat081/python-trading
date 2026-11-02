from datetime import date, timedelta

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path, reverse

from services.massive_service import MassiveAPIError, fetch_historical_data
from services.momentum_strategy import run_strategy, start_strategy

from .models import BrokerageAccount, StockData, StrategyConfig, TradeLog


class BackfillForm(forms.Form):
    symbol = forms.CharField(max_length=16, help_text="Ticker, e.g. AAPL")
    start_date = forms.DateField(initial=lambda: date.today() - timedelta(days=365))
    end_date = forms.DateField(initial=date.today)
    timespan = forms.ChoiceField(
        choices=[(c, c) for c in ("day", "hour", "minute", "week", "month")], initial="day"
    )
    multiplier = forms.IntegerField(min_value=1, initial=1)


def _backfill(modeladmin, request, symbols, days=365):
    end = date.today()
    start = end - timedelta(days=days)
    for symbol in sorted(set(symbols)):
        try:
            s = fetch_historical_data(symbol, start, end)
            modeladmin.message_user(
                request, f"{s['symbol']}: {s['created']} new candles ({s['skipped']} already stored)."
            )
        except MassiveAPIError as exc:
            modeladmin.message_user(request, f"{symbol}: backfill failed - {exc}", messages.ERROR)


@admin.register(BrokerageAccount)
class BrokerageAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "brokerage_name", "account_name", "account_id", "is_paper", "created_at")
    list_filter = ("brokerage_name", "is_paper")
    search_fields = ("user__username", "account_id", "account_name", "snaptrade_user_id")
    readonly_fields = ("created_at",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Don't echo the SnapTrade secret back in the page.
        form.base_fields["snaptrade_user_secret"].widget = forms.PasswordInput(render_value=True)
        return form


@admin.register(StockData)
class StockDataAdmin(admin.ModelAdmin):
    list_display = ("symbol", "timestamp", "open_price", "high_price", "low_price", "close_price", "volume")
    list_filter = ("symbol",)
    search_fields = ("symbol",)
    date_hierarchy = "timestamp"
    ordering = ("symbol", "-timestamp")
    actions = ["backfill_symbols"]
    change_list_template = "admin/trading/stockdata/change_list.html"

    def get_urls(self):
        custom = [
            path(
                "backfill/",
                self.admin_site.admin_view(self.backfill_view),
                name="trading_stockdata_backfill",
            )
        ]
        return custom + super().get_urls()

    def backfill_view(self, request):
        form = BackfillForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            d = form.cleaned_data
            try:
                s = fetch_historical_data(
                    d["symbol"], d["start_date"], d["end_date"],
                    timespan=d["timespan"], multiplier=d["multiplier"],
                )
                self.message_user(
                    request,
                    f"{s['symbol']}: fetched {s['fetched']}, created {s['created']}, skipped {s['skipped']}.",
                )
                return redirect(reverse("admin:trading_stockdata_changelist") + f"?symbol={s['symbol']}")
            except MassiveAPIError as exc:
                self.message_user(request, f"Backfill failed: {exc}", messages.ERROR)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "form": form,
            "title": "Backfill historical data",
        }
        return render(request, "admin/trading/stockdata/backfill.html", context)

    @admin.action(description="Backfill last 365 days for selected symbols")
    def backfill_symbols(self, request, queryset):
        _backfill(self, request, queryset.values_list("symbol", flat=True).distinct())


@admin.register(StrategyConfig)
class StrategyConfigAdmin(admin.ModelAdmin):
    list_display = (
        "name", "symbol", "is_active", "ma_type", "short_window", "long_window",
        "account", "trade_quantity", "last_signal", "trend", "last_run",
    )
    list_filter = ("is_active", "ma_type", "symbol")
    list_editable = ("is_active",)
    search_fields = ("name", "symbol")
    readonly_fields = ("last_run", "last_signal", "trend", "last_signal_bar")
    actions = ["run_selected", "evaluate_selected", "backfill_selected", "activate", "deactivate"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            # Same automatic setup as the web UI: backfill, evaluate, switch ON, trend entry.
            try:
                result = start_strategy(obj)
                self.message_user(request, f"Auto-start: {result}")
            except Exception as exc:
                self.message_user(request, f"Auto-start failed: {exc}", messages.ERROR)

    @admin.action(description="Run strategy evaluation (place orders on signals)")
    def run_selected(self, request, queryset):
        self._run(request, queryset, execute=True)

    @admin.action(description="Evaluate only (no orders)")
    def evaluate_selected(self, request, queryset):
        self._run(request, queryset, execute=False)

    def _run(self, request, queryset, execute):
        for strategy in queryset.select_related("account"):
            try:
                result = run_strategy(strategy, execute=execute)
                level = messages.SUCCESS if result.signal != "HOLD" else messages.INFO
                if result.trade_status == "FAILED":
                    level = messages.ERROR
                self.message_user(request, str(result), level)
            except Exception as exc:
                self.message_user(request, f"{strategy.name}: {exc}", messages.ERROR)

    @admin.action(description="Backfill last 365 days of data for selected strategies")
    def backfill_selected(self, request, queryset):
        _backfill(self, request, queryset.values_list("symbol", flat=True))

    @admin.action(description="Activate selected strategies")
    def activate(self, request, queryset):
        self.message_user(request, f"Activated {queryset.update(is_active=True)} strategies.")

    @admin.action(description="Deactivate selected strategies")
    def deactivate(self, request, queryset):
        self.message_user(request, f"Deactivated {queryset.update(is_active=False)} strategies.")


@admin.register(TradeLog)
class TradeLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "symbol", "action", "quantity", "price", "status", "order_id", "account", "strategy")
    list_filter = ("status", "action", "symbol")
    search_fields = ("symbol", "order_id", "message")
    readonly_fields = [f.name for f in TradeLog._meta.fields]

    def has_add_permission(self, request):
        return False
