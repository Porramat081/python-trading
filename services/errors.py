"""Exceptions shared by all broker and market-data integrations."""


class BrokerError(Exception):
    """A broker call failed (configuration problem, API error or order rejection)."""

    def __init__(self, message: str, status: int | None = None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class TradingDisabledError(BrokerError):
    """An order was attempted while the safety switches forbid it."""


class OrderSkipped(BrokerError):
    """The order was intentionally not sent (e.g. quantity below one Thai board lot)."""


class MarketDataError(Exception):
    """Price history could not be fetched."""
