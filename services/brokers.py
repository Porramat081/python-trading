"""One interface for every broker, so the strategy engine doesn't care which one is behind it.

Every broker object is bound to one BrokerageAccount and provides:
    get_balances() -> {"net_value", "currency", "cash", "buying_power", "raw"}
    get_positions() -> [{"symbol", "units", "price", "market_value", "cost_basis", ...}]
    get_position_units(symbol) -> float          # shares that can be sold now
    execute_order(symbol, side, units, ref_price=None, slippage_pct=0)
        -> {"order_id", "status", "price", "quantity", "raw"}
"""
from __future__ import annotations


class SnapTradeBroker:
    """US stocks: Alpaca through SnapTrade (market orders)."""

    market = "US"

    def __init__(self, account, service=None):
        self.account = account
        self._service = service

    @property
    def service(self):
        if self._service is None:
            from services.snaptrade_service import SnapTradeService

            self._service = SnapTradeService()
        return self._service

    def _creds(self):
        a = self.account
        return a.snaptrade_user_id, a.snaptrade_user_secret, a.account_id

    def get_balances(self) -> dict:
        return self.service.get_balances(*self._creds())

    def get_positions(self) -> list[dict]:
        return self.service.get_positions(*self._creds())

    def get_position_units(self, symbol: str) -> float:
        return self.service.get_position_units(*self._creds(), symbol)

    def execute_order(self, symbol, side, units, ref_price=None, slippage_pct=0) -> dict:
        user_id, user_secret, account_id = self._creds()
        order = self.service.execute_order(
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
            symbol=symbol,
            side=side,
            units=units,
            order_type="Market",
            is_paper=self.account.is_paper,
        )
        order.setdefault("quantity", float(units))
        return order


def get_broker(account, snaptrade_service=None, settrade_factory=None):
    """Return the broker client for an account, based on its provider."""
    if account.is_settrade:
        from services.settrade_service import SettradeService

        return SettradeService(account, investor_factory=settrade_factory)
    return SnapTradeBroker(account, service=snaptrade_service)
