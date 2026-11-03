from django.conf import settings


def trading_flags(request):
    """Expose the trading safety switches to every template."""
    return {
        "TRADING_ENABLED": settings.TRADING_ENABLED,
        "LIVE_TRADING": settings.LIVE_TRADING,
        "MARKET_OPEN_TIME": settings.MARKET_OPEN_TIME,
        "MARKET_CLOSE_TIME": settings.MARKET_CLOSE_TIME,
    }
