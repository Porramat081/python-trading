from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from services.momentum_strategy import run_strategy
from trading.models import StrategyConfig


class Command(BaseCommand):
    help = (
        "Evaluate momentum strategies and place orders on new crossover signals. "
        "Schedule with cron, e.g. `*/15 9-16 * * 1-5 python manage.py run_strategy --all`."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--symbol", help="Run active strategies for this ticker (e.g. AAPL).")
        target.add_argument("--strategy-id", type=int, help="Run one strategy by ID (even if inactive).")
        target.add_argument("--all", action="store_true", help="Run every active strategy.")
        parser.add_argument("--market", choices=["US", "TH"], help="Limit to one market.")
        parser.add_argument(
            "--backfill-days",
            type=int,
            default=0,
            help="Fetch the last N days of daily candles from Massive before evaluating.",
        )
        parser.add_argument(
            "--evaluate-only",
            action="store_true",
            help="Compute and print signals without sending any orders.",
        )

    def handle(self, *args, **opts):
        if opts["strategy_id"]:
            strategies = StrategyConfig.objects.filter(pk=opts["strategy_id"])
        elif opts["symbol"]:
            strategies = StrategyConfig.objects.filter(symbol=opts["symbol"].upper(), is_active=True)
        else:
            strategies = StrategyConfig.objects.filter(is_active=True)
        if opts["market"]:
            strategies = strategies.filter(market=opts["market"])
        strategies = list(strategies.select_related("account"))
        if not strategies:
            raise CommandError("No matching strategies found (is the strategy active?).")

        if opts["backfill_days"]:
            from services.errors import MarketDataError
            from services.market_data import backfill

            end = date.today()
            start = end - timedelta(days=opts["backfill_days"])
            done = set()
            for s in strategies:
                if (s.market, s.symbol) in done:
                    continue
                done.add((s.market, s.symbol))
                try:
                    summary = backfill(s.market, s.symbol, start, end, account=s.account)
                    self.stdout.write(f"Backfill {s.market}:{s.symbol}: {summary['created']} new candles")
                except MarketDataError as exc:
                    self.stderr.write(self.style.WARNING(f"Backfill {s.market}:{s.symbol} failed: {exc}"))

        failures = 0
        for strategy in strategies:
            try:
                result = run_strategy(strategy, execute=not opts["evaluate_only"])
            except Exception as exc:  # keep going with other strategies
                failures += 1
                self.stderr.write(self.style.ERROR(f"[{strategy.name}] error: {exc}"))
                continue
            style = self.style.SUCCESS if result.signal != "HOLD" else (lambda s: s)
            self.stdout.write(style(str(result)))
            if result.trade_status == "FAILED":
                failures += 1
        if failures:
            raise CommandError(f"{failures} strategy run(s) failed.")
