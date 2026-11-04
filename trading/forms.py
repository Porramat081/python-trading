from django import forms

from .models import BrokerageAccount, DayTradeSettings


def linked_accounts(user):
    accounts = BrokerageAccount.objects.exclude(account_id="")
    if user is not None and not user.is_superuser:
        accounts = accounts.filter(user=user)
    return accounts


class DayTradeSettingsForm(forms.ModelForm):
    class Meta:
        model = DayTradeSettings
        fields = [
            "account", "data_provider", "timeframe_minutes", "loop_minutes",
            "block_usd", "max_blocks", "max_open_positions", "stop_loss_pct",
            "limit_offset", "buy_timeout_minutes", "liquidate_at", "observe_minutes",
            "screener_source", "screener_count", "min_price", "max_price", "min_volume", "watchlist",
        ]
        labels = {
            "timeframe_minutes": "Chart timeframe", "loop_minutes": "Loop every (min)",
            "block_usd": "Block size ($)", "max_blocks": "Max blocks / ticker",
            "max_open_positions": "Max open positions", "stop_loss_pct": "Stop-loss %",
            "limit_offset": "Limit = ask + $", "buy_timeout_minutes": "Cancel unfilled buy after (min)",
            "liquidate_at": "Liquidate at (ET)", "observe_minutes": "Observe expiry (min)",
            "screener_source": "Screener", "screener_count": "Candidates", "min_price": "Min price ($)",
            "max_price": "Max price ($)", "min_volume": "Min volume today", "data_provider": "Market data",
            "watchlist": "Watchlist (fallback)",
        }
        widgets = {
            "liquidate_at": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "watchlist": forms.Textarea(attrs={"rows": 2, "placeholder": "SPY, AAPL, NVDA, TSLA"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = linked_accounts(user)
        self.fields["account"].required = False
        self.fields["account"].empty_label = "— none (simulate only) —"
        for f in self.fields.values():
            f.help_text = ""

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("loop_minutes") and not 1 <= cleaned["loop_minutes"] <= 5:
            self.add_error("loop_minutes", "Use 1–5 minutes (spec).")
        if cleaned.get("max_blocks") is not None and cleaned["max_blocks"] < 1:
            self.add_error("max_blocks", "At least 1.")
        lo, hi = cleaned.get("min_price"), cleaned.get("max_price")
        if lo is not None and hi is not None and lo >= hi:
            self.add_error("max_price", "Must be above min price.")
        if cleaned.get("screener_source") == "WATCHLIST" and not cleaned.get("watchlist", "").strip():
            self.add_error("watchlist", "Add at least one ticker, or pick another screener.")
        if cleaned.get("watchlist"):
            from services.daytrader import SYMBOL_RE, parse_watchlist

            tickers = parse_watchlist(cleaned["watchlist"])
            bad = [t for t in tickers if not SYMBOL_RE.match(t)]
            if bad:
                self.add_error("watchlist", f"Not valid tickers: {', '.join(bad)}")
            else:
                cleaned["watchlist"] = ", ".join(tickers)
        t = cleaned.get("liquidate_at")
        if t and not ("09:30" < t.strftime("%H:%M") <= "16:00"):
            self.add_error("liquidate_at", "Must be during market hours (after 09:30, by 16:00).")
        return cleaned
