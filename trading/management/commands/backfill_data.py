from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from services.massive_service import MassiveAPIError, fetch_historical_data


class Command(BaseCommand):
    help = "Backfill historical OHLCV candles from the Massive API into StockData."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", required=True, nargs="+", help="One or more tickers.")
        parser.add_argument("--days", type=int, default=365, help="Days back from today (default 365).")
        parser.add_argument("--start", type=date.fromisoformat, help="Start date YYYY-MM-DD.")
        parser.add_argument("--end", type=date.fromisoformat, help="End date YYYY-MM-DD.")
        parser.add_argument(
            "--timespan",
            default="day",
            choices=["minute", "hour", "day", "week", "month"],
            help="Candle size (default: day).",
        )
        parser.add_argument("--multiplier", type=int, default=1, help="e.g. 5 with minute = 5-min bars.")

    def handle(self, *args, **opts):
        end = opts["end"] or date.today()
        start = opts["start"] or end - timedelta(days=opts["days"])
        if start > end:
            raise CommandError("--start must be before --end")
        errors = 0
        for symbol in opts["symbol"]:
            try:
                s = fetch_historical_data(
                    symbol, start, end, timespan=opts["timespan"], multiplier=opts["multiplier"]
                )
            except MassiveAPIError as exc:
                errors += 1
                self.stderr.write(self.style.ERROR(f"{symbol.upper()}: {exc}"))
                continue
            self.stdout.write(
                self.style.SUCCESS(
                    f"{s['symbol']}: fetched {s['fetched']}, created {s['created']}, skipped {s['skipped']}"
                )
            )
        if errors:
            raise CommandError(f"{errors} symbol(s) failed.")
