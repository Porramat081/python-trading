from django import forms

from .models import BrokerageAccount, StrategyConfig


def linked_accounts(user):
    accounts = BrokerageAccount.objects.exclude(account_id="")
    if user is not None and not user.is_superuser:
        accounts = accounts.filter(user=user)
    return accounts


class StrategyConfigForm(forms.ModelForm):
    """Full form used for editing a strategy."""

    class Meta:
        model = StrategyConfig
        fields = ["symbol", "trade_quantity", "account", "ma_type", "short_window", "long_window", "name"]
        labels = {"trade_quantity": "Quantity", "ma_type": "Average", "short_window": "Short", "long_window": "Long"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = linked_accounts(user)
        self.fields["account"].queryset = accounts
        self.fields["account"].required = False
        self.fields["account"].empty_label = "— none —"
        self.fields["name"].required = False
        for f in self.fields.values():
            f.help_text = ""
        if not self.instance.pk and accounts.count() == 1:
            self.fields["account"].initial = accounts.first().pk

    def clean_symbol(self):
        return self.cleaned_data["symbol"].strip().upper()

    def clean(self):
        cleaned = super().clean()
        short, long_ = cleaned.get("short_window"), cleaned.get("long_window")
        if short and long_ and short >= long_:
            self.add_error("short_window", "Must be smaller than Long.")
        if cleaned.get("trade_quantity") is not None and cleaned["trade_quantity"] <= 0:
            self.add_error("trade_quantity", "Must be positive.")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        if not obj.name:
            obj.name = f"{obj.symbol} {obj.ma_type} {obj.short_window}/{obj.long_window}"
        if commit:
            obj.save()
        return obj
