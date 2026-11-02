import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand

from services.momentum_strategy import refresh_and_run_active


@dataclass(frozen=True)
class MarketHours:
    code: str
    tz: ZoneInfo
    sessions: tuple  # ((open, close), ...) local time
    after_close: dtime  # one extra run once the daily candle is final

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return local.weekday() < 5 and any(o <= local.time() < c for o, c in self.sessions)


MARKETS = {
    # NYSE/Nasdaq regular session.
    "US": MarketHours("US", ZoneInfo("America/New_York"), ((dtime(9, 30), dtime(16, 0)),), dtime(16, 10)),
    # SET: morning and afternoon sessions (Bangkok time).
    "TH": MarketHours(
        "TH", ZoneInfo("Asia/Bangkok"), ((dtime(10, 0), dtime(12, 30)), (dtime(14, 30), dtime(16, 30))), dtime(16, 45)
    ),
}


def market_is_open(now: datetime, market: str = "US") -> bool:
    """Exchange holidays are not included."""
    return MARKETS[market].is_open(now)


class Command(BaseCommand):
    help = (
        "Keep strategies running automatically. For each market (US, TH), during its trading "
        "hours, refresh recent candles and run every active strategy every --interval minutes, "
        "plus one final run after that market closes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=15, help="Minutes between runs (default 15).")
        parser.add_argument("--once", action="store_true", help="Run one cycle for all markets now and exit.")
        parser.add_argument("--market", choices=sorted(MARKETS), help="Only run this market.")
        parser.add_argument("--ignore-market-hours", action="store_true", help="Run every interval, 24/7.")

    def handle(self, *args, **opts):
        markets = [MARKETS[opts["market"]]] if opts["market"] else list(MARKETS.values())
        if opts["once"]:
            for m in markets:
                self._cycle(m)
            return

        interval = max(1, opts["interval"]) * 60
        names = "/".join(m.code for m in markets)
        self.stdout.write(
            f"Scheduler started for {names}: every {opts['interval']} min"
            + ("" if opts["ignore_market_hours"] else " during each market's hours + after close")
            + ". Ctrl+C to stop."
        )
        last_close_run = {}
        try:
            while True:
                for m in markets:
                    now = m.now()
                    if opts["ignore_market_hours"] or m.is_open(now):
                        self._cycle(m)
                    elif now.weekday() < 5 and now.time() >= m.after_close and last_close_run.get(m.code) != now.date():
                        self.stdout.write(f"{m.code} after-close run (final daily candle):")
                        self._cycle(m)
                        last_close_run[m.code] = now.date()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Scheduler stopped.")

    def _cycle(self, m: MarketHours):
        self.stdout.write(f"--- {m.code} {m.now():%Y-%m-%d %H:%M %Z} ---")
        results = refresh_and_run_active(log=self.stdout.write, market=m.code)
        if not results:
            self.stdout.write(f"No active {m.code} strategies.")
