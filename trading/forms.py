from django import forms

from .models import BrokerageAccount, Market, StrategyConfig


def linked_accounts(user, market: str | None = None):
    """Accounts that can trade, optionally only those for one market."""
    accounts = BrokerageAccount.objects.exclude(account_id="")
    if user is not None and not user.is_superuser:
        accounts = accounts.filter(user=user)
    if market == Market.TH:
        accounts = accounts.filter(provider=BrokerageAccount.Provider.SETTRADE)
    elif market == Market.US:
        accounts = accounts.filter(provider=BrokerageAccount.Provider.SNAPTRADE)
    return [a for a in accounts if a.is_linked]


class StrategyConfigForm(forms.ModelForm):
    """Create/edit a strategy. The market comes from the UI switch (create) or the strategy (edit)."""

    class Meta:
        model = StrategyConfig
        fields = [
            "symbol", "trade_quantity", "account", "ma_type", "short_window", "long_window",
            "slippage_pct", "name",
        ]
        labels = {
            "trade_quantity": "Quantity (shares)", "ma_type": "Average", "short_window": "Short",
            "long_window": "Long", "slippage_pct": "Limit slippage %",
        }

    def __init__(self, *args, user=None, market=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.market = self.instance.market if self.instance.pk else (market or Market.US)
        self.instance.market = self.market  # model validation compares it with the account's market
        accounts = linked_accounts(user, self.market)
        self.fields["account"].queryset = BrokerageAccount.objects.filter(pk__in=[a.pk for a in accounts])
        self.fields["account"].required = False
        self.fields["account"].empty_label = "— none —"
        self.fields["name"].required = False
        self.fields["slippage_pct"].required = False
        for f in self.fields.values():
            f.help_text = ""
        if self.market != Market.TH:
            del self.fields["slippage_pct"]
        if not self.instance.pk and len(accounts) == 1:
            self.fields["account"].initial = accounts[0].pk

    def clean_symbol(self):
        return self.cleaned_data["symbol"].strip().upper()

    def clean_slippage_pct(self):
        value = self.cleaned_data.get("slippage_pct")
        if value is None:
            return 0
        if value < 0 or value > 10:
            raise forms.ValidationError("Use 0–10%.")
        return value

    def clean(self):
        cleaned = super().clean()
        short, long_ = cleaned.get("short_window"), cleaned.get("long_window")
        if short and long_ and short >= long_:
            self.add_error("short_window", "Must be smaller than Long.")
        qty = cleaned.get("trade_quantity")
        if qty is not None and qty <= 0:
            self.add_error("trade_quantity", "Must be positive.")
        elif qty is not None and self.market == Market.TH and qty < 100:
            self.add_error("trade_quantity", "Thai orders trade in lots of 100 shares; use at least 100.")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.market = self.market
        if not obj.name:
            obj.name = f"{obj.symbol} {obj.ma_type} {obj.short_window}/{obj.long_window}"
        if commit:
            obj.save()
        return obj


class SettradeAccountForm(forms.ModelForm):
    """Settrade Open API credentials. Leave secret fields blank when editing to keep them."""

    SECRET_FIELDS = ("settrade_app_secret", "settrade_app_code", "settrade_pin")

    settrade_app_id = forms.CharField(label="App ID")
    settrade_app_secret = forms.CharField(label="App secret", required=False, widget=forms.PasswordInput)
    settrade_app_code = forms.CharField(label="App code", required=False, widget=forms.PasswordInput)
    settrade_pin = forms.CharField(label="Trading PIN", required=False, widget=forms.PasswordInput)

    class Meta:
        model = BrokerageAccount
        fields = [
            "account_name", "account_id", "settrade_broker_id", "settrade_app_id",
            "settrade_app_secret", "settrade_app_code", "settrade_pin",
        ]
        labels = {"account_name": "Name", "account_id": "Trading account no.", "settrade_broker_id": "Broker ID"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account_id"].required = True
        self.fields["settrade_broker_id"].required = True
        self.fields["account_name"].required = False
        editing = bool(self.instance.pk)
        for name in self.SECRET_FIELDS:
            self.fields[name].required = not editing
            if editing:
                self.fields[name].widget.attrs["placeholder"] = "•••••• (unchanged)"
        for f in self.fields.values():
            f.help_text = ""

    def clean_settrade_broker_id(self):
        return self.cleaned_data["settrade_broker_id"].strip().upper()

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.provider = BrokerageAccount.Provider.SETTRADE
        if self.instance.pk:
            original = BrokerageAccount.objects.get(pk=self.instance.pk)
            for name in self.SECRET_FIELDS:
                if not self.cleaned_data.get(name):
                    setattr(obj, name, getattr(original, name))
        if not obj.account_name:
            obj.account_name = f"Settrade {obj.account_id}"
        if commit:
            obj.save()
        return obj
