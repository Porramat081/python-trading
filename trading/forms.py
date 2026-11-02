from django import forms

from .models import BrokerageAccount, StrategyConfig


class StrategyConfigForm(forms.ModelForm):
    class Meta:
        model = StrategyConfig
        fields = ["name", "symbol", "is_active", "ma_type", "short_window", "long_window", "account", "trade_quantity"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = BrokerageAccount.objects.exclude(account_id="")
        if user is not None and not user.is_superuser:
            accounts = accounts.filter(user=user)
        self.fields["account"].queryset = accounts
        self.fields["account"].required = False

    def clean_symbol(self):
        return self.cleaned_data["symbol"].strip().upper()

    def clean(self):
        cleaned = super().clean()
        short, long_ = cleaned.get("short_window"), cleaned.get("long_window")
        if short and long_ and short >= long_:
            self.add_error("short_window", "Short window must be smaller than long window.")
        if cleaned.get("trade_quantity") is not None and cleaned["trade_quantity"] <= 0:
            self.add_error("trade_quantity", "Quantity must be positive.")
        return cleaned


class BackfillForm(forms.Form):
    days = forms.IntegerField(min_value=1, max_value=3650, initial=365)
