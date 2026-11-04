import logging
from datetime import datetime, time as dtime, timedelta

import pandas as pd
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from services import indicators as ind
from services.daytrader import NEW_YORK, DayTrader, MassiveData
from services.snaptrade_service import SnapTradeService, SnapTradeServiceError

from .forms import DayTradeSettingsForm
from .models import BrokerageAccount, DayTradeSettings, ObserveItem, Position, ScanResult, TradeLog

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


def _today_start():
    """Midnight New York time today (as an aware datetime)."""
    now = timezone.now().astimezone(NEW_YORK)
    return datetime.combine(now.date(), dtime.min, tzinfo=NEW_YORK)


@login_required
def dashboard(request):
    cfg = DayTradeSettings.load()
    accounts = list(_user_accounts(request.user))
    linked = [a for a in accounts if a.is_linked]
    service = _service_or_none(request) if linked else None

    account_cards, total_value = [], 0.0
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

    today = _today_start()
    open_positions = list(Position.objects.filter(status__in=["PENDING", "OPEN"]))
    closed_today = list(Position.objects.filter(status="CLOSED", closed_at__gte=today))
    realized = sum(p.pnl or 0 for p in closed_today)
    unrealized = sum(p.pnl or 0 for p in open_positions)

    observe = []
    held = {p.symbol for p in open_positions}
    for item in ObserveItem.objects.all():
        owned = item.symbol in held
        expires = None if owned else item.added_at + timedelta(minutes=cfg.observe_minutes)
        observe.append({"item": item, "owned": owned, "expires": expires})

    last_scan = ScanResult.objects.order_by("-scanned_at").first()
    scan_rows = (
        ScanResult.objects.filter(scanned_at__gte=last_scan.scanned_at - timedelta(minutes=5)) if last_scan else []
    )
    chart_symbols = sorted(held | {o["item"].symbol for o in observe})
    selected = request.GET.get("symbol") or (chart_symbols[0] if chart_symbols else "")

    context = {
        "cfg": cfg,
        "account_cards": account_cards,
        "has_pending_registration": any(not a.is_linked for a in accounts),
        "total_value": total_value,
        "open_positions": open_positions,
        "closed_today": closed_today,
        "realized": realized,
        "unrealized": unrealized,
        "observe": observe,
        "scan_rows": scan_rows,
        "last_scan": last_scan,
        "recent_trades": TradeLog.objects.select_related("position")[:12],
        "chart_symbols": chart_symbols,
        "selected_symbol": selected.upper(),
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


# -- strategy settings & actions ----------------------------------------------------
@login_required
def strategy(request):
    cfg = DayTradeSettings.load()
    form = DayTradeSettingsForm(request.POST or None, instance=cfg, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Strategy settings saved.")
        return redirect("trading:strategy")
    return render(request, "trading/strategy.html", {"form": form, "cfg": cfg})


def _report(request, report, title):
    lines = report.actions or ["No actions."]
    messages.info(request, f"{title}: " + " · ".join(lines)[:1500])


@login_required
@require_POST
def toggle_auto(request):
    cfg = DayTradeSettings.load()
    cfg.auto_trading = not cfg.auto_trading
    cfg.save(update_fields=["auto_trading"])
    messages.info(request, f"Automatic day trading is now {'ON' if cfg.auto_trading else 'OFF'}.")
    return redirect(request.POST.get("next") or "trading:dashboard")


@login_required
@require_POST
def scan_now(request):
    trader = DayTrader()
    try:
        trader.scan()
    except Exception as exc:
        logger.exception("Scan failed")
        messages.error(request, f"Scan failed: {exc}")
    else:
        _report(request, trader.report, "Scan")
    return redirect(request.POST.get("next") or "trading:dashboard")


@login_required
@require_POST
def run_now(request):
    trader = DayTrader()
    try:
        trader.run_cycle(force=True)
    except Exception as exc:
        logger.exception("Cycle failed")
        messages.error(request, f"Loop failed: {exc}")
    else:
        _report(request, trader.report, "Loop")
    return redirect(request.POST.get("next") or "trading:dashboard")


@login_required
@require_POST
def liquidate_now(request):
    trader = DayTrader()
    trader.liquidate_all(reason="MANUAL")
    _report(request, trader.report, "Close all")
    return redirect(request.POST.get("next") or "trading:dashboard")


@login_required
@require_POST
def position_close(request, pk):
    pos = get_object_or_404(Position, pk=pk, status="OPEN")
    trader = DayTrader()
    trader.exit(pos, "MANUAL", "Closed from dashboard")
    _report(request, trader.report, pos.symbol)
    return redirect("trading:dashboard")


@login_required
@require_POST
def observe_remove(request, pk):
    item = get_object_or_404(ObserveItem, pk=pk)
    item.delete()
    messages.info(request, f"{item.symbol} removed from observe list.")
    return redirect("trading:dashboard")


@login_required
def chart(request, symbol):
    """5/15-min closes with 9/21 EMA for the dashboard chart (fetched live from Massive)."""
    cfg = DayTradeSettings.load()
    try:
        bars = MassiveData().intraday(symbol.upper(), cfg.timeframe_minutes, timezone.now())
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    closes = bars.closed.tail(200)
    if closes.empty:
        return JsonResponse({"labels": [], "close": [], "ema_fast": [], "ema_slow": [], "symbol": symbol.upper()})
    frame = ind.compute_frame(bars.closed).tail(len(closes))
    tz = timezone.get_current_timezone()

    def clean(series):
        return [None if pd.isna(v) else round(float(v), 4) for v in series]

    return JsonResponse(
        {
            "symbol": symbol.upper(),
            "timeframe": cfg.timeframe_minutes,
            "labels": [ts.astimezone(tz).strftime("%m-%d %H:%M") for ts in frame.index],
            "close": clean(frame["close"]),
            "ema_fast": clean(frame["ema_fast"]),
            "ema_slow": clean(frame["ema_slow"]),
            "rsi": clean(frame["rsi"]),
            "macd_hist": clean(frame["macd_hist"]),
        }
    )


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
