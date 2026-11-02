"""SnapTrade SDK wrapper: user registration, Alpaca onboarding, portfolio data and orders."""
from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Callable

from django.conf import settings

logger = logging.getLogger(__name__)


from services.errors import BrokerError, TradingDisabledError  # noqa: E402,F401 (re-exported)


class SnapTradeServiceError(BrokerError):
    """Raised for configuration problems or errors returned by SnapTrade / the brokerage."""


def to_plain(value: Any) -> Any:
    """Convert SDK schema objects (frozendict / tuple / Decimal subclasses) to plain Python."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return str(value)
    # NoneClass / BoolClass schema objects
    if hasattr(value, "is_none_oapg") and value.is_none_oapg():
        return None
    if hasattr(value, "is_true_oapg"):
        return bool(value.is_true_oapg())
    return value


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


class SnapTradeService:
    """High-level SnapTrade operations used by views, admin actions and strategies."""

    def __init__(
        self,
        client_id: str | None = None,
        consumer_key: str | None = None,
        client: Any = None,
        max_retries: int = 4,
    ):
        self.max_retries = max_retries
        if client is not None:
            self.client = client
            return

        client_id = client_id or settings.SNAPTRADE_CLIENT_ID
        consumer_key = consumer_key or settings.SNAPTRADE_CONSUMER_KEY
        if not client_id or not consumer_key:
            raise SnapTradeServiceError(
                "SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY must be configured."
            )
        from snaptrade_client import SnapTrade
        from snaptrade_client.auth import SnapTradeAuth

        if getattr(settings, "SNAPTRADE_KEY_TYPE", "commercial") == "personal":
            auth = SnapTradeAuth.personal_api_key(consumer_key=consumer_key, client_id=client_id)
        else:
            auth = SnapTradeAuth.commercial_api_key(consumer_key=consumer_key, client_id=client_id)
        self.client = SnapTrade(auth=auth)

    # -- error handling / retries ------------------------------------------
    def _call(self, description: str, func: Callable, **kwargs) -> Any:
        """Call an SDK method with retry on 429 / 5xx and return the plain response body."""
        from snaptrade_client.exceptions import ApiException

        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                response = func(**kwargs)
                return to_plain(getattr(response, "body", response))
            except ApiException as exc:
                status = getattr(exc, "status", None)
                retryable = status == 429 or (status is not None and status >= 500)
                if retryable and attempt < self.max_retries:
                    headers = getattr(exc, "headers", None) or {}
                    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
                    try:
                        wait = float(retry_after) if retry_after else backoff
                    except ValueError:
                        wait = backoff
                    wait += random.uniform(0, 0.5)  # jitter
                    logger.warning(
                        "SnapTrade %s returned %s; retrying in %.1fs", description, status, wait
                    )
                    time.sleep(wait)
                    backoff *= 2
                    continue
                body = to_plain(getattr(exc, "body", None))
                detail = body.get("detail") if isinstance(body, dict) else body
                raise SnapTradeServiceError(
                    f"SnapTrade {description} failed ({status}): {detail or exc.reason}",
                    status=status,
                    body=body,
                ) from exc
            except Exception as exc:  # network errors, schema errors, ...
                if attempt < self.max_retries and _is_network_error(exc):
                    logger.warning("SnapTrade %s network error: %s; retrying", description, exc)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise SnapTradeServiceError(f"SnapTrade {description} failed: {exc}") from exc
        raise SnapTradeServiceError(f"SnapTrade {description} failed.")  # pragma: no cover

    # -- authentication / onboarding ---------------------------------------
    def register_user(self, user_id: str | None = None) -> dict:
        """Register a SnapTrade user. Returns {"user_id", "user_secret"}."""
        user_id = user_id or f"algo-{uuid.uuid4()}"
        body = self._call(
            "register user", self.client.authentication.register_snap_trade_user, user_id=user_id
        )
        return {"user_id": body.get("userId", user_id), "user_secret": body["userSecret"]}

    def get_connection_link(
        self,
        user_id: str,
        user_secret: str,
        redirect_url: str | None = None,
        broker: str | None = None,
        reconnect: str | None = None,
    ) -> str:
        """Return a SnapTrade Connection Portal URL for linking an Alpaca account."""
        kwargs = {
            "user_id": user_id,
            "user_secret": user_secret,
            "broker": broker or settings.SNAPTRADE_BROKER_SLUG,
            "immediate_redirect": True,
            "connection_type": "trade",
        }
        redirect_url = redirect_url or settings.SNAPTRADE_REDIRECT_URL
        if redirect_url:
            kwargs["custom_redirect"] = redirect_url
        if reconnect:
            kwargs["reconnect"] = reconnect
        body = self._call("login user", self.client.authentication.login_snap_trade_user, **kwargs)
        url = body.get("redirectURI") if isinstance(body, dict) else None
        if not url:
            raise SnapTradeServiceError(f"SnapTrade did not return a redirect URI: {body}")
        return url

    # -- portfolio data ------------------------------------------------------
    def list_accounts(self, user_id: str, user_secret: str) -> list[dict]:
        """Return the user's connected accounts as simple dicts."""
        body = self._call(
            "list accounts",
            self.client.account_information.list_all_user_accounts,
            user_id=user_id,
            user_secret=user_secret,
        )
        results = body.get("results", []) if isinstance(body, dict) else body or []
        accounts = []
        for acc in results:
            net_value = acc.get("net_value") or {}
            accounts.append(
                {
                    "id": acc.get("id"),
                    "name": acc.get("display_name") or acc.get("masked_account_number") or "",
                    "number": acc.get("masked_account_number", ""),
                    "is_paper": bool(acc.get("is_paper")),
                    "status": acc.get("status"),
                    "connection_id": acc.get("connection_id"),
                    "net_value": _to_float(net_value.get("amount")),
                    "currency": net_value.get("currency"),
                    "raw": acc,
                }
            )
        return accounts

    def get_balances(self, user_id: str, user_secret: str, account_id: str) -> dict:
        """Return {"net_value", "currency", "cash", "buying_power", "raw"}."""
        body = self._call(
            "get balances",
            self.client.account_information.get_account_balances_v2,
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
        )
        net = body.get("net_value") or {}
        cash_items = body.get("cash") or []
        cash = sum(_to_float(item.get("amount")) or 0 for item in cash_items)
        buying_power = body.get("buying_power")
        if isinstance(buying_power, list):
            bp_value = sum(_to_float(i.get("amount")) or 0 for i in buying_power)
        elif isinstance(buying_power, dict):
            bp_value = _to_float(buying_power.get("amount"))
            if bp_value is None and isinstance(buying_power.get("items"), list):
                bp_value = sum(_to_float(i.get("amount")) or 0 for i in buying_power["items"])
        else:
            bp_value = None
        return {
            "net_value": _to_float(net.get("amount")),
            "currency": net.get("currency") or (cash_items[0].get("currency") if cash_items else "USD"),
            "cash": cash,
            "buying_power": bp_value,
            "raw": body,
        }

    def get_positions(self, user_id: str, user_secret: str, account_id: str) -> list[dict]:
        """Return open positions as [{"symbol", "units", "price", "cost_basis", "market_value"}]."""
        body = self._call(
            "get positions",
            self.client.account_information.get_all_account_positions,
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
        )
        results = body.get("results", []) if isinstance(body, dict) else body or []
        positions = []
        for pos in results:
            if pos.get("cash_equivalent"):
                continue
            instrument = pos.get("instrument") or {}
            units = _to_float(pos.get("units")) or 0.0
            price = _to_float(pos.get("price"))
            positions.append(
                {
                    "symbol": instrument.get("symbol") or instrument.get("raw_symbol") or "?",
                    "description": instrument.get("description") or "",
                    "units": units,
                    "price": price,
                    "cost_basis": _to_float(pos.get("cost_basis")),
                    "market_value": units * price if price is not None else None,
                    "currency": pos.get("currency"),
                }
            )
        return positions

    def get_position_units(self, user_id: str, user_secret: str, account_id: str, symbol: str) -> float:
        symbol = symbol.upper()
        return sum(
            p["units"]
            for p in self.get_positions(user_id, user_secret, account_id)
            if str(p["symbol"]).upper() == symbol
        )

    def get_recent_orders(self, user_id: str, user_secret: str, account_id: str) -> list[dict]:
        body = self._call(
            "get orders",
            self.client.account_information.get_user_account_orders,
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
            state="all",
            days=30,
        )
        return body if isinstance(body, list) else body.get("orders", []) if isinstance(body, dict) else []

    # -- trading -------------------------------------------------------------
    def execute_order(
        self,
        user_id: str,
        user_secret: str,
        account_id: str,
        symbol: str,
        side: str,
        units: float | Decimal,
        order_type: str = "Market",
        time_in_force: str = "Day",
        limit_price: float | None = None,
        is_paper: bool | None = None,
    ) -> dict:
        """Place an order through SnapTrade.

        Safety: refuses unless TRADING_ENABLED is set, and refuses non-paper accounts
        unless LIVE_TRADING is set. Returns {"order_id", "status", "price", "raw"}.
        """
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise SnapTradeServiceError(f"Unsupported order side: {side}")
        if not settings.TRADING_ENABLED:
            raise TradingDisabledError("TRADING_ENABLED is False; order not sent (dry run).")
        if is_paper is False and not settings.LIVE_TRADING:
            raise TradingDisabledError(
                "Account is a live (real-money) account but LIVE_TRADING is False; order refused."
            )
        units = float(units)
        if units <= 0:
            raise SnapTradeServiceError("Order units must be positive.")

        kwargs = {
            "user_id": user_id,
            "user_secret": user_secret,
            "account_id": account_id,
            "action": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "symbol": symbol.upper(),
            "units": units,
            "client_order_id": str(uuid.uuid4()),
        }
        if order_type in {"Limit", "StopLimit"}:
            if limit_price is None:
                raise SnapTradeServiceError("limit_price is required for limit orders.")
            kwargs["price"] = float(limit_price)

        # Orders are never retried automatically on 5xx to avoid duplicate fills;
        # only 429 (request rejected before processing) is retried.
        body = self._call_order(**kwargs)
        return {
            "order_id": str(body.get("brokerage_order_id") or body.get("id") or ""),
            "status": str(body.get("status") or ""),
            "price": _to_float(body.get("execution_price") or body.get("limit_price")),
            "raw": body,
        }

    def _call_order(self, **kwargs) -> dict:
        from snaptrade_client.exceptions import ApiException

        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.trading.place_force_order(**kwargs)
                return to_plain(response.body)
            except ApiException as exc:
                if exc.status == 429 and attempt < self.max_retries:
                    time.sleep(backoff + random.uniform(0, 0.5))
                    backoff *= 2
                    continue
                body = to_plain(getattr(exc, "body", None))
                detail = body.get("detail") if isinstance(body, dict) else body
                raise SnapTradeServiceError(
                    f"Brokerage rejected order ({exc.status}): {detail or exc.reason}",
                    status=exc.status,
                    body=body,
                ) from exc
            except Exception as exc:
                raise SnapTradeServiceError(f"Order placement failed: {exc}") from exc
        raise SnapTradeServiceError("Order placement failed after retries.")


def _is_network_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return any(k in name for k in ("timeout", "connection", "protocol", "maxretry"))


def execute_order(user_id, account_id, symbol, side, units, order_type="Market", **kwargs) -> dict:
    """Spec-level helper: look up the stored secret for `user_id` and place an order."""
    from trading.models import BrokerageAccount

    account = BrokerageAccount.objects.get(snaptrade_user_id=user_id, account_id=account_id)
    return SnapTradeService().execute_order(
        user_id=user_id,
        user_secret=account.snaptrade_user_secret,
        account_id=account_id,
        symbol=symbol,
        side=side,
        units=units,
        order_type=order_type,
        is_paper=account.is_paper,
        **kwargs,
    )
