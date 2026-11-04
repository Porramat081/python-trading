import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand

from services.daytrader import DayTrader
from trading.models import DayTradeSettings

NY = ZoneInfo("America/New_York")
MARKET_OPEN = dtime.fromisoformat(settings.MARKET_OPEN_TIME)
MARKET_CLOSE = dtime.fromisoformat(settings.MARKET_CLOSE_TIME)


def market_is_open(now: datetime) -> bool:
    """Regular US session, Mon-Fri 09:30-16:00 New York time (exchange holidays not included)."""
    now = now.astimezone(NY)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


def _sleep_seconds(now: datetime, interval_minutes: int, liquidate_at: dtime) -> float:
    """Sleep one interval, but wake up exactly at the liquidation time if it comes sooner."""
    seconds = interval_minutes * 60
    target = now.replace(hour=liquidate_at.hour, minute=liquidate_at.minute, second=0, microsecond=0)
    until = (target - now).total_seconds()
    if 0 < until < seconds:
        return until + 1
    return seconds


class Command(BaseCommand):
    help = (
        "Run the day-trading loop: every N minutes during US market hours (N = 'loop minutes' "
        "in Strategy settings, 1-5), monitor holdings, the observe list and the morning screener; "
        "liquidate everything at the liquidation time (default 3:45 PM ET)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, help="Override minutes between loops.")
        parser.add_argument("--once", action="store_true", help="Run one loop now and exit.")
        parser.add_argument("--ignore-market-hours", action="store_true", help="Run loops even when closed.")

    def handle(self, *args, **opts):
        if opts["once"]:
            self._cycle(force=opts["ignore_market_hours"])
            return

        self.stdout.write("Day-trading scheduler started (Ctrl+C to stop).")
        try:
            while True:
                cfg = DayTradeSettings.load()
                interval = max(1, opts["interval"] or cfg.loop_minutes)
                now = datetime.now(NY)
                if not cfg.auto_trading:
                    pass  # paused from the UI; keep checking the switch
                elif opts["ignore_market_hours"] or market_is_open(now):
                    self._cycle(force=opts["ignore_market_hours"])
                time.sleep(_sleep_seconds(datetime.now(NY), interval, cfg.liquidate_at))
        except KeyboardInterrupt:
            self.stdout.write("Scheduler stopped.")

    def _cycle(self, force=False):
        self.stdout.write(f"--- {datetime.now(NY):%Y-%m-%d %H:%M:%S %Z} ---")
        try:
            report = DayTrader().run_cycle(force=force)
            self.stdout.write(str(report))
        except Exception as exc:  # keep the loop alive
            self.stderr.write(self.style.ERROR(f"Cycle failed: {exc}"))
