import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand

from services.momentum_strategy import refresh_and_run_active

NY = ZoneInfo("America/New_York")
MARKET_OPEN = dtime.fromisoformat(settings.MARKET_OPEN_TIME)
MARKET_CLOSE = dtime.fromisoformat(settings.MARKET_CLOSE_TIME)
AFTER_CLOSE = dtime(16, 10)  # daily candle is final shortly after the close


def market_is_open(now: datetime) -> bool:
    """Regular US session, Mon-Fri 09:30-16:00 New York time (exchange holidays not included)."""
    now = now.astimezone(NY)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


class Command(BaseCommand):
    help = (
        "Keep strategies running automatically: during US market hours, refresh recent "
        "candles and run every active strategy every --interval minutes, plus one final "
        "run after the close. Leave it running next to `runserver`."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=15, help="Minutes between runs (default 15).")
        parser.add_argument("--once", action="store_true", help="Run one cycle now and exit.")
        parser.add_argument(
            "--ignore-market-hours", action="store_true", help="Run every interval, 24/7."
        )

    def handle(self, *args, **opts):
        interval = max(1, opts["interval"]) * 60
        if opts["once"]:
            self._cycle()
            return

        self.stdout.write(
            f"Scheduler started: every {opts['interval']} min"
            + ("" if opts["ignore_market_hours"] else " during US market hours + after close")
            + ". Ctrl+C to stop."
        )
        last_close_run = None
        try:
            while True:
                now = datetime.now(NY)
                if opts["ignore_market_hours"] or market_is_open(now):
                    self._cycle()
                elif now.weekday() < 5 and now.time() >= AFTER_CLOSE and last_close_run != now.date():
                    self.stdout.write("After-close run (final daily candle):")
                    self._cycle()
                    last_close_run = now.date()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Scheduler stopped.")

    def _cycle(self):
        stamp = datetime.now(NY).strftime("%Y-%m-%d %H:%M %Z")
        self.stdout.write(f"--- {stamp} ---")
        results = refresh_and_run_active(log=self.stdout.write)
        if not results:
            self.stdout.write("No active strategies.")
