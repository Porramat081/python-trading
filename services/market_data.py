"""Market-aware price data: storage of candles and backfill routing (US -> Massive, TH -> Settrade)."""
from __future__ import annotations

import logging
from datetime import date

from services.errors import BrokerError, MarketDataError

logger = logging.getLogger(__name__)


def store_candles(market: str, symbol: str, candles: list) -> dict:
    """Insert candles into StockData, skipping existing rows but refreshing the latest stored one
    (it may have been saved mid-session as a partial bar). Returns a summary dict."""
    from trading.models import StockData

    symbol = symbol.upper()
    if not candles:
        return {"symbol": symbol, "market": market, "fetched": 0, "created": 0, "skipped": 0}
    candles = sorted(candles, key=lambda c: c.timestamp)
    existing = set(
        StockData.objects.filter(
            market=market,
            symbol=symbol,
            timestamp__gte=candles[0].timestamp,
            timestamp__lte=candles[-1].timestamp,
        ).values_list("timestamp", flat=True)
    )
    rows = [
        StockData(
            market=market,
            symbol=symbol,
            timestamp=c.timestamp,
            open_price=c.open,
            high_price=c.high,
            low_price=c.low,
            close_price=c.close,
            volume=c.volume,
        )
        for c in candles
    ]
    new_rows = [r for r in rows if r.timestamp not in existing]
    StockData.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)
    for r in [r for r in rows if r.timestamp in existing][-1:]:
        StockData.objects.filter(market=market, symbol=symbol, timestamp=r.timestamp).update(
            open_price=r.open_price, high_price=r.high_price, low_price=r.low_price,
            close_price=r.close_price, volume=r.volume,
        )
    summary = {
        "symbol": symbol,
        "market": market,
        "fetched": len(candles),
        "created": len(new_rows),
        "skipped": len(candles) - len(new_rows),
    }
    logger.info("Backfill %s:%s: %s", market, symbol, summary)
    return summary


def data_account_for(market: str, account=None):
    """Settrade market data needs credentials: use the strategy's account or any linked one."""
    from trading.models import BrokerageAccount

    if account is not None and account.is_settrade and account.is_linked:
        return account
    for acc in BrokerageAccount.objects.filter(provider=BrokerageAccount.Provider.SETTRADE):
        if acc.is_linked:
            return acc
    return None


def backfill(market: str, symbol: str, start: date, end: date, account=None) -> dict:
    """Fetch daily candles for `symbol` in `market` and store them. Raises MarketDataError."""
    if market == "TH":
        from services.settrade_service import SettradeService

        data_account = data_account_for(market, account)
        if data_account is None:
            raise MarketDataError(
                "Thai price data needs a Settrade account. Add one on the Settings page."
            )
        try:
            candles = SettradeService(data_account).get_daily_candles(symbol, start, end)
        except BrokerError as exc:
            raise MarketDataError(str(exc)) from exc
        return store_candles("TH", symbol, candles)

    from services.massive_service import fetch_historical_data

    return fetch_historical_data(symbol, start, end)
