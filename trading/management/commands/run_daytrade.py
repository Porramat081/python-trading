from django.core.management.base import BaseCommand

from services.daytrader import DayTrader


class Command(BaseCommand):
    help = "Day-trading actions on demand: one monitoring loop, a screener scan, or liquidate all."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--scan", action="store_true", help="Run the screener (Case 1) now.")
        group.add_argument("--liquidate", action="store_true", help="Sell all bot positions now.")
        parser.add_argument("--force", action="store_true", help="Run even when the market is closed.")

    def handle(self, *args, **opts):
        trader = DayTrader()
        if opts["scan"]:
            trader.scan()
        elif opts["liquidate"]:
            trader.liquidate_all(reason="MANUAL")
        else:
            trader.run_cycle(force=opts["force"])
        self.stdout.write(str(trader.report))
