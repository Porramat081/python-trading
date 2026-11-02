import logging
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from services.momentum_strategy import chart_data, run_strategy, start_strategy
from services.snaptrade_service import SnapTradeService, SnapTradeServiceError

from .forms import StrategyConfigForm, linked_accounts
from .models import BrokerageAccount, StockData, StrategyConfig, TradeLog

logger = logging.getLogger(__name__)


def _user_accounts(user):
    qs = BrokerageAccount.objects.all() if user.is_superuser else BrokerageAccount.objects.filter(user=user)
    return qs.order_by("brokerage_name", "account_name")


def _service_or_none(request):
    try:
        return SnapTradeService()
    except SnapTradeServiceError as exc:
        messages.warning(request, str(exc))
        return None


@login_required
def dashboard(request):
    accounts = list(_user_accounts(request.user))
    linked = [a for a in accounts if a.is_linked]
    service = _service_or_none(request) if linked else None

    account_cards = []
    total_value = 0.0
    for account in linked:
        card = {"account": account, "balance": None, "error": None}
        if service:
            try:
                card["balance"] = service.get_balances(
                    account.snaptrade_user_id, account.snaptrade_user_secret, account.account_id
                )
                total_value += card["balance"]["net_value"] or 0
            except SnapTradeServiceError as exc:
                card["error"] = str(exc)
        account_cards.append(card)

    strategies = list(StrategyConfig.objects.select_related("account").order_by("name"))
    selected_id = request.GET.get("strategy")
    selected = next((s for s in strategies if str(s.pk) == selected_id), strategies[0] if strategies else None)

    context = {
        "account_cards": account_cards,
        "has_pending_registration": any(not a.is_linked for a in accounts),
        "total_value": total_value,
        "recent_trades": TradeLog.objects.filter(account__in=accounts).select_related("account", "strategy")[:10],
        "strategies": strategies,
        "selected_strategy": selected,
        "candle_count": StockData.objects.filter(symbol=selected.symbol).count() if selected else 0,
    }
    return render(request, "trading/dashboard.html", context)


@login_required
def positions(request):
    accounts = [a for a in _user_accounts(request.user) if a.is_linked]
    service = _service_or_none(request) if accounts else None
    rows = []
    for account in accounts:
        entry = {"account": account, "positions": [], "error": None}
        if service:
            try:
                entry["positions"] = service.get_positions(
                    account.snaptrade_user_id, account.snaptrade_user_secret, account.account_id
                )
            except SnapTradeServiceError as exc:
                entry["error"] = str(exc)
        rows.append(entry)
    has_positions = any(r["positions"] for r in rows)
    return render(request, "trading/positions.html", {"rows": rows, "has_positions": has_positions})


# -- strategies -------------------------------------------------------------
@login_required
def strategies(request):
    if request.method == "POST":
        form = StrategyConfigForm(request.POST, user=request.user)
        if form.is_valid():
            strategy = form.save()
            # Automatic setup: backfill -> evaluate -> switch ON -> trend-based entry.
            try:
                result = start_strategy(strategy)
                level = messages.ERROR if result.trade_status == "FAILED" else messages.SUCCESS
                messages.add_message(request, level, f"{strategy.symbol}: {result.signal} · {result.message}")
            except Exception as exc:
                logger.exception("Strategy start failed")
                messages.error(request, f"{strategy.symbol} created, but automatic start failed: {exc}")
            return redirect("trading:strategies")
    else:
        form = StrategyConfigForm(user=request.user, initial={"short_window": 10, "long_window": 50})
    items = StrategyConfig.objects.select_related("account").order_by("symbol", "name")
    return render(
        request,
        "trading/strategies.html",
        {"strategies": items, "form": form, "multi_account": linked_accounts(request.user).count() > 1},
    )


@login_required
def strategy_edit(request, pk):
    strategy = get_object_or_404(StrategyConfig, pk=pk)
    form = StrategyConfigForm(request.POST or None, instance=strategy, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Strategy '{strategy.name}' updated.")
        return redirect("trading:strategies")
    return render(request, "trading/strategy_edit.html", {"form": form, "strategy": strategy})


@login_required
@require_POST
def strategy_toggle(request, pk):
    strategy = get_object_or_404(StrategyConfig, pk=pk)
    strategy.is_active = not strategy.is_active
    strategy.save(update_fields=["is_active"])
    state = "ON" if strategy.is_active else "OFF"
    messages.info(request, f"Automated execution for '{strategy.name}' is now {state}.")
    return redirect(request.POST.get("next") or "trading:strategies")


@login_required
@require_POST
def strategy_delete(request, pk):
    strategy = get_object_or_404(StrategyConfig, pk=pk)
    strategy.delete()
    messages.info(request, f"Strategy '{strategy.name}' deleted.")
    return redirect("trading:strategies")


@login_required
@require_POST
def strategy_run(request, pk):
    strategy = get_object_or_404(StrategyConfig.objects.select_related("account"), pk=pk)
    try:
        result = run_strategy(strategy, execute=True)
        level = messages.ERROR if result.trade_status == "FAILED" else messages.SUCCESS
        messages.add_message(request, level, str(result))
    except Exception as exc:
        logger.exception("Strategy run failed")
        messages.error(request, f"Run failed: {exc}")
    return redirect(request.POST.get("next") or "trading:strategies")


@login_required
def strategy_chart(request, pk):
    strategy = get_object_or_404(StrategyConfig, pk=pk)
    try:
        limit = min(int(request.GET.get("limit", 250)), 2000)
    except ValueError:
        limit = 250
    data = chart_data(strategy, limit=limit)
    data.update(
        {
            "symbol": strategy.symbol,
            "ma_type": strategy.ma_type,
            "short_window": strategy.short_window,
            "long_window": strategy.long_window,
        }
    )
    return JsonResponse(data)


# -- brokerage onboarding (SnapTrade) ----------------------------------------
@login_required
@require_POST
def connect_brokerage(request):
    """Register the user with SnapTrade (once) and redirect to the Connection Portal."""
    service = _service_or_none(request)
    if service is None:
        return redirect("trading:dashboard")

    existing = BrokerageAccount.objects.filter(user=request.user).first()
    try:
        if existing:
            user_id, user_secret = existing.snaptrade_user_id, existing.snaptrade_user_secret
        else:
            creds = service.register_user(f"django-{request.user.pk}-{request.user.username}")
            user_id, user_secret = creds["user_id"], creds["user_secret"]
            BrokerageAccount.objects.create(
                user=request.user,
                snaptrade_user_id=user_id,
                snaptrade_user_secret=user_secret,
                brokerage_name="Alpaca",
                is_paper=not settings.LIVE_TRADING,
            )
        redirect_url = settings.SNAPTRADE_REDIRECT_URL or request.build_absolute_uri(
            reverse("trading:connect_callback")
        )
        link = service.get_connection_link(user_id, user_secret, redirect_url=redirect_url)
    except SnapTradeServiceError as exc:
        messages.error(request, f"Could not start brokerage connection: {exc}")
        return redirect("trading:dashboard")
    return redirect(link)


@login_required
def connect_callback(request):
    """SnapTrade redirects here after the user links Alpaca; sync the accounts."""
    _sync_accounts(request)
    return redirect("trading:dashboard")


@login_required
@require_POST
def sync_accounts(request):
    _sync_accounts(request)
    return redirect(request.POST.get("next") or "trading:dashboard")


def _sync_accounts(request):
    service = _service_or_none(request)
    creds = BrokerageAccount.objects.filter(user=request.user).first()
    if service is None or creds is None:
        messages.warning(request, "No SnapTrade user registered yet. Click 'Connect Alpaca' first.")
        return
    try:
        remote = service.list_accounts(creds.snaptrade_user_id, creds.snaptrade_user_secret)
    except SnapTradeServiceError as exc:
        messages.error(request, f"Could not fetch accounts: {exc}")
        return

    with transaction.atomic():
        seen = []
        for acc in remote:
            obj, created = BrokerageAccount.objects.update_or_create(
                user=request.user,
                snaptrade_user_id=creds.snaptrade_user_id,
                account_id=acc["id"],
                defaults={
                    "snaptrade_user_secret": creds.snaptrade_user_secret,
                    "account_name": acc["name"],
                    "brokerage_name": "Alpaca",
                    "is_paper": acc["is_paper"],
                },
            )
            seen.append(obj.pk)
        if seen:
            # The placeholder row (registration only, no account yet) is no longer needed.
            BrokerageAccount.objects.filter(
                user=request.user, snaptrade_user_id=creds.snaptrade_user_id, account_id=""
            ).delete()
    if remote:
        messages.success(request, f"Synced {len(remote)} brokerage account(s).")
    else:
        messages.info(request, "No accounts linked yet. Complete the connection in SnapTrade.")
