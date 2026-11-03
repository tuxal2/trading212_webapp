"""Data classes for the responses of the Trading212 web API.

Amounts are kept as ``Decimal`` (the API returns values such as ``0E-10``).
Every object additionally keeps the original response in ``raw``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


def _d(value: Any) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class Account:
    id: int
    type: str                 # LIVE | DEMO
    trading_type: str         # EQUITY | CFD | ISA | ...
    currency: str
    status: str
    dealer: str
    ready_to_trade: bool
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "Account":
        return cls(
            id=int(d["id"]),
            type=d.get("type", ""),
            trading_type=d.get("tradingType", ""),
            currency=d.get("currencyCode", ""),
            status=d.get("status", ""),
            dealer=d.get("accountDealer", ""),
            ready_to_trade=bool(d.get("readyToTrade", False)),
            raw=d,
        )


@dataclass
class LoginResult:
    """Result of ``POST /rest/v5/login`` or ``/rest/v4/login/token``."""

    account_id: int
    customer_id: int | None
    customer_uuid: str | None
    email: str
    trading_type: str
    sub_system: str           # LIVE | DEMO
    currency: str
    account_session: str = field(repr=False)
    customer_session: str = field(repr=False)
    session_cookie_name: str
    customer_cookie_name: str
    login_token: str | None = field(repr=False, default=None)
    remember_me_cookie: dict | None = field(repr=False, default=None)
    accounts: list[Account] = field(default_factory=list)
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "LoginResult":
        account = d.get("account") or {}
        customer = d.get("customer") or {}
        return cls(
            account_id=int(d.get("accountId") or account.get("id")),
            customer_id=customer.get("id"),
            customer_uuid=d.get("customerId") or customer.get("uuid"),
            email=d.get("email", ""),
            trading_type=d.get("tradingType") or account.get("tradingType", "EQUITY"),
            sub_system=d.get("subSystem") or account.get("type", "LIVE"),
            currency=account.get("currencyCode", "EUR"),
            account_session=d["accountSession"],
            customer_session=d.get("customerSession") or d["accountSession"],
            session_cookie_name=d.get("sessionCookieName") or "TRADING212_SESSION_LIVE",
            customer_cookie_name=d.get("customerCookieName") or "CUSTOMER_SESSION",
            login_token=d.get("loginToken"),
            remember_me_cookie=d.get("rememberMeCookie"),
            accounts=[Account.from_json(a) for a in d.get("accounts", [])],
            raw=d,
        )


@dataclass
class Cash:
    """``cash`` block of ``/rest/v1/equity/multi-accounts/summary``."""

    currency: str
    total: Decimal               # total account value (cash + positions)
    free_for_stocks: Decimal     # free to invest
    invest_pot: Decimal
    spending_pot: Decimal        # card / spending balance
    blocked_for_stocks: Decimal  # blocked by open orders
    pie_cash: Decimal
    stock_investment: Decimal    # capital invested in positions
    ppl: Decimal                 # unrealised profit/loss
    result: Decimal              # realised profit/loss
    net_deposit: Decimal         # net deposits
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict, currency: str) -> "Cash":
        return cls(
            currency=currency,
            total=_d(d.get("total")),
            free_for_stocks=_d(d.get("freeForStocks")),
            invest_pot=_d(d.get("investPot")),
            spending_pot=_d(d.get("spendingPot")),
            blocked_for_stocks=_d(d.get("blockedForStocks")),
            pie_cash=_d(d.get("pieCash")),
            stock_investment=_d(d.get("stockInvestment")),
            ppl=_d(d.get("ppl")),
            result=_d(d.get("result")),
            net_deposit=_d(d.get("netDeposit")),
            raw=d,
        )


@dataclass
class Wallet:
    """A currency wallet from ``/rest/trading/v1/account/wallets``."""

    currency: str
    primary: bool
    free_for_stocks: Decimal
    invest_pot: Decimal
    spending_pot: Decimal
    blocked_for_stocks: Decimal
    converted_currency: str | None = None
    converted_free_for_stocks: Decimal | None = None
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, currency: str, d: dict) -> "Wallet":
        conv = d.get("converted") or {}
        return cls(
            currency=currency,
            primary=bool(d.get("primary", False)),
            free_for_stocks=_d(d.get("freeForStocks")),
            invest_pot=_d(d.get("investPot")),
            spending_pot=_d(d.get("spendingPot")),
            blocked_for_stocks=_d(d.get("blockedForStocks")),
            converted_currency=conv.get("currencyCode"),
            converted_free_for_stocks=_d(conv["freeForStocks"]) if "freeForStocks" in conv else None,
            raw=d,
        )


@dataclass
class Budget:
    """Combined budget: account cash + wallets per currency."""

    account_id: int
    cash: Cash
    wallets: list[Wallet]

    @property
    def available(self) -> Decimal:
        """Capital free to invest (in account currency)."""
        return self.cash.free_for_stocks


# --------------------------------------------------------------------------- Orders

ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT")
TIME_VALIDITIES = ("DAY", "GOOD_TILL_CANCEL")


@dataclass
class Order:
    """Open order (``equityOrders`` / ``orders`` in the account responses).

    ``quantity`` is negative for sell orders.
    """

    order_id: str
    ticker: str
    type: str
    side: str                    # BUY | SELL
    quantity: Decimal | None
    filled_quantity: Decimal
    value: Decimal | None        # for value (amount) orders
    limit_price: Decimal | None
    stop_price: Decimal | None
    currency: str
    status: str
    time_validity: str
    extended_hours: bool
    created: str | None
    frontend: str | None
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "Order":
        qty = d.get("quantity")
        val = d.get("value")
        sign_src = qty if qty is not None else val
        side = "SELL" if sign_src is not None and _d(sign_src) < 0 else "BUY"
        return cls(
            order_id=str(d.get("orderId") or d.get("id")),
            ticker=d.get("code") or d.get("ticker", ""),
            type=d.get("type", ""),
            side=side,
            quantity=_d(qty) if qty is not None else None,
            filled_quantity=_d(d.get("filledQuantity")),
            value=_d(val) if val is not None else None,
            limit_price=_d(d["limitPrice"]) if d.get("limitPrice") is not None else None,
            stop_price=_d(d["stopPrice"]) if d.get("stopPrice") is not None else None,
            currency=d.get("currencyCode", ""),
            status=d.get("status", ""),
            time_validity=d.get("timeValidity", ""),
            extended_hours=bool(d.get("enabledExtendedMarketHours", False)),
            created=d.get("created"),
            frontend=d.get("frontend"),
            raw=d,
        )


@dataclass
class OrderCosts:
    """Response of ``/rest/v2/public/added-costs`` or ``value-order/review``."""

    quantity: Decimal
    order_value: Decimal
    shares_value: Decimal
    total: Decimal
    exchange_rate: Decimal | None        # FX rate if settlement currency ≠ quote currency
    rate_from: str | None                # e.g. USD
    rate_to: str | None                  # e.g. EUR
    costs: dict                          # e.g. {"CURRENCY_CONVERSION_FEE": 8.15, "FINRA_FEE": 0.02}
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "OrderCosts":
        return cls(
            quantity=_d(d.get("orderQuantity")),
            order_value=_d(d.get("orderValue")),
            shares_value=_d(d.get("sharesValue")),
            total=_d(d.get("total")),
            exchange_rate=_rate(d.get("exchangeRate")),
            rate_from=(d.get("exchangeRate") or {}).get("fromCurrency") if isinstance(d.get("exchangeRate"), dict) else None,
            rate_to=(d.get("exchangeRate") or {}).get("toCurrency") if isinstance(d.get("exchangeRate"), dict) else None,
            costs=d.get("costs") or {},
            raw=d,
        )


def _rate(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return _d(v["rate"]) if v.get("rate") is not None else None
    return _d(v)


@dataclass
class InstrumentSettings:
    """``/rest/v1/equity/order/instruments/settings`` – limits for an instrument."""

    ticker: str
    max_buy: Decimal
    max_sell: Decimal
    min_trade: Decimal
    max_pending_distance_percent: Decimal
    suspended: bool
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "InstrumentSettings":
        return cls(
            ticker=d.get("code", ""),
            max_buy=_d(d.get("maxBuy")),
            max_sell=_d(d.get("maxSell")),
            min_trade=_d(d.get("minTrade")),
            max_pending_distance_percent=_d(d.get("maxPendingDistancePercent")),
            suspended=bool(d.get("suspended", False)),
            raw=d,
        )


@dataclass
class Instrument:
    """Instrument search hit."""

    ticker: str
    short_name: str
    name: str
    full_name: str
    category: str                # EQUITY | CFD | CRYPTO | ...
    type: str
    currency: str | None
    isin: str | None
    exchange: str | None
    tradable: bool
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "Instrument":
        state = d.get("state") or {}
        hl = d.get("_highlightResult") or {}

        def attr(key: str) -> str | None:
            if d.get(key):
                return d[key]
            v = hl.get(key)
            return v.get("value") if isinstance(v, dict) else None

        return cls(
            ticker=d.get("objectID", ""),
            short_name=d.get("shortName", ""),
            name=d.get("name", ""),
            full_name=d.get("fullName", ""),
            category=d.get("category") or ("EQUITY" if d.get("objectID", "").endswith("_EQ") else ""),
            type=d.get("type", ""),
            currency=attr("currencyCode") or ("USD" if "_US_" in d.get("objectID", "") else None),
            isin=attr("isin"),
            exchange=d.get("exchangeName"),
            tradable=bool(state.get("tradable", True)) and not state.get("suspended", False),
            raw=d,
        )
