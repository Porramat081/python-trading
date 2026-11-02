from django.conf import settings

from .models import MARKET_SYMBOL, Market


def trading_flags(request):
    """Expose the trading safety switches and the selected market to every template."""
    market = Market.US
    if hasattr(request, "session"):
        market = request.session.get("market", Market.US)
        if market not in Market.values:
            market = Market.US
    return {
        "TRADING_ENABLED": settings.TRADING_ENABLED,
        "LIVE_TRADING": settings.LIVE_TRADING,
        "MARKET": market,
        "CUR": MARKET_SYMBOL[market],
        "MARKETS": Market.choices,
    }
