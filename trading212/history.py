"""History, order details, taxes and profit/loss.

Endpoints (from captures of the web client):

  GET  /rest/history/types?dealer=T212DE                        → ["all","orders","dividends",...]
  GET  /rest/history/v2/{type}?limit=20[&olderThan=…][&instrumentCode=…]
  GET  /rest/history/v5/{type}/{id}                              details (e.g. /orders/99319640949)
  GET  /rest/taxes/v2/overview                                   exemption order, loss pots, return
  GET  /rest/taxes/v1/pot-summary/pot/{STOCK_LOSS_POT|LOSS_POT|WITHHOLDING_TAX_POT}
  GET  /rest/customer-taxes/v1/customers/exemption/nv-status
  POST /rest/reports/v1/income-taxes/tax-transactions/search?limit=20[&idCursor=…&dateTimeCursor=…]
  GET  /rest/reports/v1/income-taxes/tax-transactions/EQUITY/{taxType}/{incomeId}
  GET  /rest/document-central/v3/documents/categories/by-name/ANNUAL_STATEMENT

The history responses are UI building blocks (``{"key": "history.order.sold.full", "context": {...}}``).
This module turns them into data classes with numbers and readable labels and keeps the
original data in ``raw``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator

from .exceptions import ApiError

HISTORY_TYPES = ("all", "orders", "dividends", "transactions", "interest")
LOSS_POTS = {"stock": "STOCK_LOSS_POT", "other": "LOSS_POT", "withholding": "WITHHOLDING_TAX_POT"}

ENTRY_LABELS = {
    "history.order.sold.full": "Sell",
    "history.order.bought.full": "Buy",
    "history.order.sold.partial": "Sell (partial)",
    "history.order.bought.partial": "Buy (partial)",
    "history.order.bought.cancelled": "Buy cancelled",
    "history.order.sold.cancelled": "Sell cancelled",
    "history.dividend.heading": "Dividend",
    "history.interest-on-cash.heading": "Interest",
    "history.card-cashback.new": "Card cashback",
    "history.card-debit.new": "Card payment",
    "history.transaction.heading.deposit": "Deposit",
    "history.transaction.heading.withdrawal": "Withdrawal",
    "history.currency-exchange": "Currency exchange",
}

DETAIL_LABELS = {
    "history.details.tradeConfirmation.label": "Trade confirmation",
    "history.details.order.status.key": "Status",
    "history.details.order.id.key": "Order ID",
    "history.details.order.date-created.key": "Created",
    "history.details.order.limit-price.key": "Limit",
    "history.details.order.stop-price.key": "Stop",
    "history.details.order.enteredAmount.quantity": "Quantity (order)",
    "history.details.order.enteredAmount.value": "Value (order)",
    "history.details.order.fill.quantity.key": "Filled quantity",
    "history.details.order.fill.price.key": "Fill price",
    "history.details.order.fill.type.key": "Fill type",
    "history.details.order.fill.id.key": "Fill ID",
    "history.details.totalBeforeTax": "Total before tax",
    "history.details.taxes": "Taxes",
    "history.details.costBasis": "Cost basis",
    "history.details.amountReceived": "Received",
    "history.details.amountPaid": "Paid",
    "history.details.return": "Profit/loss",
    "history.details.fees": "Fees",
    "history.details.exchange-rate.label": "Exchange rate",
}

VALUE_LABELS = {
    "history.order.status.filled": "filled",
    "history.order.status.cancelled": "cancelled",
    "history.order.status.rejected": "rejected",
    "history.order.status.partially-filled": "partially filled",
    "history.details.order.fill.type.otc": "over the counter (OTC)",
    "history.details.order.fill.type.exchange": "exchange",
    "history.tradeConfirmation.download": "PDF",
}

SUMMARY_LABELS = {
    "history.footer.return.description": "Profit/loss",
    "history.footer.dividends.description": "Dividends",
    "history.footer.transactions.positive.description": "Incoming",
    "history.footer.transactions.negative.description": "Outgoing",
    "history.footer.interest.description": "Interest",
}

TAX_TYPE_LABELS = {
    "EQUITY_SOLD": "Sale",
    "INTEREST_ON_CASH": "Interest",
    "DIVIDEND": "Dividend",
}


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _label(key: str | None, table: dict[str, str]) -> str:
    if not key:
        return ""
    if key in table:
        return table[key]
    return key.rsplit(".", 1)[-1].replace("-", " ").replace("_", " ")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # truncate nanoseconds (Python supports at most 6 fractional digits)
    if "." in s:
        head, tail = s.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(frac):]
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------- history list

@dataclass
class HistoryEntry:
    date: str
    kind: str                 # ORDER | DIVIDEND | INTEREST_ON_CASH | TRANSACTION | CARD_TRANSACTION | ...
    status: str | None        # BUY | SELL | CANCELLED | REJECTED | EARNING | ...
    label: str                # readable label (Buy, Sell, Dividend, ...)
    ticker: str | None
    name: str | None
    amount: Decimal | None    # sign as in the app: buy negative, sell/dividend positive
    currency: str | None
    quantity: Decimal | None
    details_type: str | None  # e.g. "orders" → for get_history_details
    details_id: str | None
    category: str | None = None            # e.g. merchant category for card payments
    second_amount: Decimal | None = None   # e.g. target amount for a currency exchange
    second_currency: str | None = None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def datetime(self) -> datetime | None:
        return _parse_dt(self.date)

    @classmethod
    def from_json(cls, d: dict) -> "HistoryEntry":
        heading = d.get("heading") or {}
        sub = d.get("subHeading") or {}
        main = (d.get("mainInfo") or {}).get("context") or {}
        add = d.get("additionalInfo") or {}
        add_ctx = add.get("context") or {}
        avatar = d.get("avatar") or {}
        hctx = heading.get("context") or {}

        if heading.get("key") == "history.instrument":
            label = _label(sub.get("key"), ENTRY_LABELS)
        else:
            label = _label(heading.get("key"), ENTRY_LABELS)
        if avatar.get("status") == "REJECTED":
            label = label.replace("cancelled", "rejected")

        path = (d.get("detailsPath") or "").strip("/")
        dtype, _, did = path.partition("/")
        second_amount = second_cur = None
        if "currency-amount" in (add.get("key") or ""):
            second_amount, second_cur = _d(add_ctx.get("amount")), add_ctx.get("currency")
        return cls(
            date=d.get("date", ""),
            kind=avatar.get("type", ""),
            status=avatar.get("status"),
            label=label,
            ticker=hctx.get("instrumentCode") or (avatar.get("additionalInfo")
                                                  if avatar.get("type") in ("ORDER", "DIVIDEND") else None),
            category=None if avatar.get("type") in ("ORDER", "DIVIDEND") else avatar.get("additionalInfo"),
            name=hctx.get("prettyName"),
            amount=_d(main.get("amount")),
            currency=main.get("currency"),
            quantity=_d(add_ctx.get("quantity")) if "quantity" in add_ctx else None,
            details_type=dtype or None,
            details_id=did or None,
            second_amount=second_amount,
            second_currency=second_cur,
            raw=d,
        )


@dataclass
class SummaryValue:
    label: str
    amount: Decimal
    currency: str


def parse_summary(header: dict | None) -> list[SummaryValue]:
    out = []
    for row in (header or {}).get("data", []) or []:
        ctx = (row.get("value") or {}).get("context") or {}
        if "amount" in ctx:
            out.append(SummaryValue(_label((row.get("description") or {}).get("key"), SUMMARY_LABELS),
                                    _d(ctx["amount"]), ctx.get("currency", "")))
    return out


# --------------------------------------------------------------------------- details

@dataclass
class DetailRow:
    section: str          # section (e.g. "Profit/loss"), empty if it has no heading
    label: str            # readable label
    text: str             # formatted value
    key: str              # original key of the description
    value_key: str
    context: dict
    is_total: bool = False


def _fmt_amount(v: Any, cur: str | None = None) -> str:
    d = _d(v)
    if d is None:
        return "-"
    s = f"{d:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {cur}".strip() if cur else s


def _value_text(value: dict) -> str:
    key = value.get("key") or ""
    ctx = value.get("context") or {}
    if key in VALUE_LABELS:
        return VALUE_LABELS[key]
    if "two-currencies" in key:
        a = _fmt_amount(ctx.get("accountAmount"), ctx.get("accountCurrency"))
        b = _fmt_amount(ctx.get("instrumentAmount"), ctx.get("instrumentCurrency"))
        return f"{a} ({b})"
    if "withPercent" in key:
        return f"{_fmt_amount(ctx.get('amount'), ctx.get('currency'))} ({_fmt_amount(ctx.get('rate'))} %)"
    if "amount" in ctx:
        return _fmt_amount(ctx["amount"], ctx.get("currency"))
    if "quantity" in ctx:
        q = _d(ctx["quantity"])
        return format(q.normalize(), "f").replace(".", ",") if q is not None else "-"
    if "date" in ctx:
        dt = _parse_dt(ctx["date"])
        return dt.strftime("%d.%m.%Y %H:%M:%S") if dt else ctx["date"]
    if "id" in ctx:
        return str(ctx["id"])
    return _label(key, VALUE_LABELS)


@dataclass
class HistoryDetails:
    """Details of a history item (order, dividend, tax transaction …)."""

    title: str
    date: str | None
    ticker: str | None
    side: str | None                   # BUY | SELL | EARNING ...
    amount: Decimal | None
    currency: str | None
    rows: list[DetailRow]
    # frequently used fields (None if not present)
    status: str | None = None
    order_id: str | None = None
    created: str | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    fill_price: Decimal | None = None
    price_currency: str | None = None
    fill_type: str | None = None
    fill_id: str | None = None
    taxes: Decimal | None = None
    proceeds: dict[str, Decimal] = field(default_factory=dict)     # total before tax per currency
    cost_basis: dict[str, Decimal] = field(default_factory=dict)   # cost basis per currency (negative)
    return_amount: Decimal | None = None                           # profit/loss as shown by the app
    return_currency: str | None = None
    return_percent: Decimal | None = None
    trade_confirmation: str | None = None                          # path to the PDF
    raw: dict = field(repr=False, default_factory=dict)

    def gain(self, currency: str) -> Decimal | None:
        """Profit/loss in ``currency`` = proceeds + cost basis (cost basis is negative)."""
        if currency in self.proceeds and currency in self.cost_basis:
            return self.proceeds[currency] + self.cost_basis[currency]
        return None

    @classmethod
    def from_json(cls, d: dict) -> "HistoryDetails":
        heading = d.get("heading") or {}
        title = heading.get("title") or {}
        tctx = title.get("context") or {}
        val = heading.get("value") or {}
        avatar = heading.get("avatar") or {}
        sub_ctx = (heading.get("subtitle") or {}).get("context") or {}
        det = cls(
            title=_label(title.get("key"), {
                "V2history.details.order.limit.sell": f"Limit sell {tctx.get('ticker', '')}",
                "V2history.details.order.limit.buy": f"Limit buy {tctx.get('ticker', '')}",
                "V2history.details.order.market.sell": f"Market sell {tctx.get('ticker', '')}",
                "V2history.details.order.market.buy": f"Market buy {tctx.get('ticker', '')}",
                "V2history.details.order.stop.sell": f"Stop sell {tctx.get('ticker', '')}",
                "V2history.details.order.stop.buy": f"Stop buy {tctx.get('ticker', '')}",
            }).strip(),
            date=sub_ctx.get("date"),
            ticker=avatar.get("additionalInfo"),
            side=avatar.get("status"),
            amount=_d(val.get("amount")),
            currency=val.get("currency"),
            rows=[],
            raw=d,
        )
        for sec in d.get("sections") or []:
            sec_key = (sec.get("heading") or {}).get("key")
            sec_label = _label(sec_key, DETAIL_LABELS) if sec_key else ""
            for t in sec.get("totals") or []:
                v = t.get("value") or {}
                ctx = v.get("context") or {}
                det.rows.append(DetailRow(sec_label, sec_label or "Total", _value_text(v),
                                          (t.get("description") or {}).get("key", ""), v.get("key", ""), ctx, True))
                if sec_key == "history.details.return" and "amount" in ctx:
                    det.return_amount = _d(ctx["amount"])
                    det.return_currency = ctx.get("currency")
                    det.return_percent = _d(ctx.get("rate"))
            for r in sec.get("rows") or []:
                dk = (r.get("description") or {}).get("key", "")
                v = r.get("value") or {}
                ctx = v.get("context") or {}
                det.rows.append(DetailRow(sec_label, _label(dk, DETAIL_LABELS), _value_text(v), dk,
                                          v.get("key", ""), ctx))
                det._extract(sec_key, dk, v, ctx)
                for sr in r.get("subRows") or []:
                    sdk = (sr.get("description") or {}).get("key", "")
                    sv = sr.get("value") or {}
                    det.rows.append(DetailRow(sec_label, "  " + _label(sdk, DETAIL_LABELS), _value_text(sv),
                                              sdk, sv.get("key", ""), sv.get("context") or {}))
        return det

    def _extract(self, sec_key: str | None, dk: str, v: dict, ctx: dict) -> None:
        vk = v.get("key") or ""
        if dk == "history.details.order.status.key":
            self.status = _label(vk, VALUE_LABELS)
        elif dk == "history.details.order.id.key":
            self.order_id = str(ctx.get("id"))
        elif dk == "history.details.order.date-created.key":
            self.created = ctx.get("date")
        elif dk == "history.details.order.limit-price.key":
            self.limit_price, self.price_currency = _d(ctx.get("amount")), ctx.get("currency")
        elif dk == "history.details.order.stop-price.key":
            self.stop_price, self.price_currency = _d(ctx.get("amount")), ctx.get("currency")
        elif dk == "history.details.order.enteredAmount.quantity":
            self.quantity = _d(ctx.get("quantity"))
        elif dk == "history.details.order.fill.quantity.key":
            self.filled_quantity = _d(ctx.get("quantity"))
        elif dk == "history.details.order.fill.price.key":
            self.fill_price = _d(ctx.get("amount"))
            self.price_currency = self.price_currency or ctx.get("currency")
        elif dk == "history.details.order.fill.type.key":
            self.fill_type = _label(vk, VALUE_LABELS)
        elif dk == "history.details.order.fill.id.key":
            self.fill_id = str(ctx.get("id"))
        elif dk == "history.details.taxes":
            self.taxes = _d(ctx.get("amount"))
        elif dk == "history.details.tradeConfirmation.label":
            self.trade_confirmation = ((v.get("navigation") or {}).get("url"))
        elif dk in ("history.details.totalBeforeTax", "history.details.costBasis"):
            target = self.cost_basis if dk == "history.details.costBasis" else self.proceeds
            if "two-currencies" in vk:
                for a, c in (("accountAmount", "accountCurrency"), ("instrumentAmount", "instrumentCurrency")):
                    if ctx.get(c) and ctx.get(a) is not None:
                        target[ctx[c]] = _d(ctx[a])
            elif "amount" in ctx and ctx.get("currency"):
                target[ctx["currency"]] = _d(ctx["amount"])


# --------------------------------------------------------------------------- taxes

@dataclass
class TaxOverview:
    tax_year: int | None
    exemption_limit: Decimal | None        # saver's allowance (exemption order)
    exemption_used: Decimal | None
    has_exemption: bool
    stock_loss_pot: Decimal | None         # stock loss pot
    other_loss_pot: Decimal | None         # other loss pot
    withholding_tax_pot: Decimal | None    # pot of creditable withholding tax
    taxes_paid: Decimal | None
    total_return: Decimal | None           # taxable result in the tax year
    offsets_used: Decimal | None           # losses offset
    loss_certificate_status: str | None    # loss certificate: NOT_REQUESTED | REQUESTED | INELIGIBLE
    loss_certificate_deadline: str | None
    nv_certificate: dict | None
    tax_id: str | None = field(repr=False, default=None)
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def exemption_remaining(self) -> Decimal | None:
        if self.exemption_limit is None or self.exemption_used is None:
            return None
        return self.exemption_limit - self.exemption_used

    @classmethod
    def from_json(cls, d: dict) -> "TaxOverview":
        ex = d.get("exemption") or {}
        pots = d.get("offsetPots") or {}
        lc = d.get("lossCertificate") or {}
        return cls(
            tax_year=d.get("taxYear"),
            exemption_limit=_d(ex.get("limit")),
            exemption_used=_d(ex.get("used")),
            has_exemption=bool(ex.get("hasActiveExemption")),
            stock_loss_pot=_d(pots.get("stockLossPot")),
            other_loss_pot=_d(pots.get("otherLossPot")),
            withholding_tax_pot=_d(pots.get("withholdingTaxPot")),
            taxes_paid=_d(d.get("taxesPaid")),
            total_return=_d(d.get("totalReturn")),
            offsets_used=_d(d.get("offsetsUsed")),
            loss_certificate_status=lc.get("status"),
            loss_certificate_deadline=lc.get("endTimestamp"),
            nv_certificate=d.get("nvCertificate"),
            tax_id=d.get("taxId"),
            raw=d,
        )


@dataclass
class LossPot:
    pot: str
    total_available: Decimal | None
    total_used: Decimal | None
    previous_year: Decimal | None
    current_year: Decimal | None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def remaining(self) -> Decimal | None:
        if self.total_available is None or self.total_used is None:
            return None
        return self.total_available - self.total_used

    @classmethod
    def from_json(cls, pot: str, d: dict) -> "LossPot":
        br = d.get("breakdown") or {}
        return cls(pot, _d(d.get("totalAvailable")), _d(d.get("totalUsed")),
                   _d(br.get("previousYear")), _d(br.get("currentYear")), d)


@dataclass
class TaxTransaction:
    tax_id: int
    income_id: str
    account_type: str
    tax_type: str            # EQUITY_SOLD | INTEREST_ON_CASH | DIVIDEND | ...
    label: str
    time_created: str
    tax_amount: Decimal
    currency: str
    ticker: str | None
    quantity: Decimal | None
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "TaxTransaction":
        ctx = (d.get("description") or {}).get("context") or {}
        return cls(
            tax_id=int(d["taxId"]),
            income_id=str(d["incomeId"]),
            account_type=d.get("accountType", "EQUITY"),
            tax_type=d.get("taxActionType", ""),
            label=TAX_TYPE_LABELS.get(d.get("taxActionType", ""), d.get("taxActionType", "")),
            time_created=d.get("timeCreated", ""),
            tax_amount=_d(d.get("taxAmount")) or Decimal("0"),
            currency=d.get("currency", ""),
            ticker=d.get("ticker"),
            quantity=_d(ctx.get("quantity")),
            raw=d,
        )


@dataclass
class RealizedGain:
    date: str
    ticker: str
    quantity: Decimal | None
    proceeds: dict[str, Decimal]        # proceeds per currency
    cost_basis: dict[str, Decimal]      # cost basis per currency (negative)
    gain: dict[str, Decimal]            # profit/loss per currency
    gain_percent: Decimal | None
    taxes: Decimal | None
    income_id: str


@dataclass
class Position:
    ticker: str
    quantity: Decimal
    average_price: Decimal          # average cost price in quote currency
    value: Decimal                  # current value in account currency
    investment: Decimal             # cost basis in account currency
    ppl: Decimal                    # unrealised P/L in account currency
    fx_ppl: Decimal | None          # thereof FX effect
    sellable: Decimal | None
    autoinvest_quantity: Decimal | None
    created: str | None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def ppl_percent(self) -> Decimal | None:
        return (self.ppl / self.investment * 100) if self.investment else None

    @property
    def first_buy(self) -> datetime | None:
        """First buy of the current position (``created``; resets after a full sale)."""
        try:
            return datetime.fromisoformat(self.created) if self.created else None
        except ValueError:
            return None

    def held_days(self, today: date | None = None) -> int | None:
        """Whole days since the first buy."""
        fb = self.first_buy
        if fb is None:
            return None
        return ((today or date.today()) - fb.astimezone().date()).days

    @classmethod
    def from_json(cls, d: dict) -> "Position":
        z = Decimal("0")
        return cls(
            ticker=d.get("code", ""),
            quantity=_d(d.get("quantity")) or z,
            average_price=_d(d.get("averagePrice")) or z,
            value=_d(d.get("value")) or z,
            investment=_d(d.get("investment")) or z,
            ppl=_d(d.get("ppl")) or z,
            fx_ppl=_d(d.get("fxPpl")),
            sellable=_d(d.get("sellableQuantity")),
            autoinvest_quantity=_d(d.get("autoInvestQuantity")),
            created=d.get("created"),
            raw=d,
        )


# --------------------------------------------------------------------------- client mixin

def _iso(d: date | datetime | str | None, end: bool = False) -> str | None:
    if d is None:
        return None
    if isinstance(d, str):
        return d
    if isinstance(d, datetime):
        return d.isoformat()
    return f"{d.isoformat()}T{'23:59:59.999' if end else '00:00:00.000'}Z"


def _bound(v: date | datetime | str | None, *, end: bool) -> datetime | None:
    """Time bound as an aware datetime. Plain dates are interpreted in local time
    (tax year = calendar year in Germany), ``end=True`` → end of the day."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        if len(v) == 10:
            v = date.fromisoformat(v)
        else:
            v = _parse_dt(v)
            if v is None:
                raise ValueError(f"Invalid date: {v!r}")
    if isinstance(v, datetime):
        return v if v.tzinfo else v.astimezone()
    t = datetime.max.time() if end else datetime.min.time()
    return datetime.combine(v, t).astimezone()


class HistoryMixin:
    """Wird in :class:`Trading212Client` eingemischt (nutzt ``_get``/``_post``)."""

    # -------------------------------------------------- history
    def get_history_types(self) -> list[str]:
        return self._get("/rest/history/types", params={"dealer": self._dealer()})  # type: ignore[attr-defined]

    def get_history_page(self, kind: str = "all", *, limit: int = 20, older_than: str | None = None,
                         instrument: str | None = None) -> tuple[list[HistoryEntry], bool, list[SummaryValue]]:
        """One page of the history: ``(entries, has_next, summary)``."""
        if kind not in HISTORY_TYPES:
            raise ValueError(f"kind must be one of {HISTORY_TYPES}")
        params: dict[str, Any] = {"limit": limit}
        if older_than:
            params["olderThan"] = older_than
        if instrument:
            params["instrumentCode"] = instrument
        data = self._get(f"/rest/history/v2/{kind}", params=params)  # type: ignore[attr-defined]
        entries = [HistoryEntry.from_json(x) for x in data.get("data", [])]
        return entries, bool(data.get("hasNext")), parse_summary(data.get("header"))

    def iter_history(self, kind: str = "all", *, instrument: str | None = None,
                     since: date | datetime | str | None = None, max_items: int | None = None,
                     page_size: int = 20) -> Iterator[HistoryEntry]:
        """All entries (newest first), loading page by page like the app (``olderThan``).

        ``since``: older entries are not loaded. ``max_items``: upper limit.
        """
        since_dt = _bound(since, end=False)
        seen: set[str] = set()
        older = None
        count = 0
        while True:
            entries, has_next, _ = self.get_history_page(kind, limit=page_size, older_than=older,
                                                         instrument=instrument)
            for e in entries:
                key = f"{e.details_type}/{e.details_id}/{e.date}"
                if key in seen:
                    continue
                seen.add(key)
                dt = e.datetime
                if since_dt and dt and dt.astimezone() < since_dt:
                    return
                yield e
                count += 1
                if max_items and count >= max_items:
                    return
            if not has_next or not entries:
                return
            older = entries[-1].date

    def get_history_summary(self, kind: str = "orders") -> list[SummaryValue]:
        """Totals from the history header (e.g. profit/loss, total dividends)."""
        return self.get_history_page(kind, limit=1)[2]

    def get_history_details(self, ref: str | HistoryEntry, kind: str = "orders") -> HistoryDetails:
        """Details of an entry: ``HistoryEntry``, ``"/orders/99319640949"`` or just the id."""
        if isinstance(ref, HistoryEntry):
            kind, ident = ref.details_type or kind, ref.details_id
        else:
            ref = ref.strip("/")
            if "/" in ref:
                kind, ident = ref.split("/", 1)
            else:
                ident = ref
        return HistoryDetails.from_json(self._get(f"/rest/history/v5/{kind}/{ident}"))  # type: ignore[attr-defined]

    def download_trade_confirmation(self, details: HistoryDetails | str) -> bytes:
        """Trade confirmation as PDF (path from the details, e.g. ``/v1/statements/trade-confirmation/…``).

        Note: the download itself was not part of the capture; the path is requested under
        ``/rest`` like the other app endpoints.
        """
        path = details.trade_confirmation if isinstance(details, HistoryDetails) else details
        if not path:
            raise ValueError("No trade confirmation available")
        if not path.startswith("/rest/"):
            path = "/rest" + path
        self._require_login()  # type: ignore[attr-defined]
        resp = self._request("GET", path, headers=self._auth_headers())  # type: ignore[attr-defined]
        return resp.content

    # -------------------------------------------------- taxes
    def get_tax_overview(self) -> TaxOverview:
        """Tax overview of the current year (exemption order, loss pots, result)."""
        return TaxOverview.from_json(self._get("/rest/taxes/v2/overview"))  # type: ignore[attr-defined]

    def get_loss_pot(self, pot: str = "stock") -> LossPot:
        """Loss pot: ``"stock"`` (stocks), ``"other"`` (other), ``"withholding"`` (withholding tax)."""
        name = LOSS_POTS.get(pot.lower(), pot.upper())
        return LossPot.from_json(name, self._get(f"/rest/taxes/v1/pot-summary/pot/{name}"))  # type: ignore[attr-defined]

    def get_nv_status(self) -> str:
        """Nichtveranlagungsbescheinigung: z. B. ``"NONE"``."""
        return self._get("/rest/customer-taxes/v1/customers/exemption/nv-status").get(  # type: ignore[attr-defined]
            "nvCertificateHolder", "")

    def get_annual_statements(self, page: int = 1, page_size: int = 15) -> list[dict]:
        """Jahressteuerbescheinigungen (Dokumentenliste, roh)."""
        data = self._get("/rest/document-central/v3/documents/categories/by-name/ANNUAL_STATEMENT",  # type: ignore[attr-defined]
                         params={"page": page, "pageSize": page_size})
        return data.get("documents", [])

    def iter_tax_transactions(self, *, tax_types: list[str] | None = None, tickers: list[str] | None = None,
                              date_from: date | datetime | str | None = None,
                              date_to: date | datetime | str | None = None,
                              account_types: list[str] | None = None,
                              page_size: int = 20, server_filter: bool = False) -> Iterator[TaxTransaction]:
        """Tax-relevant transactions (sales, interest, dividends …), newest first.

        As in the web client, the search is sent with an empty filter ``{}``; type, ticker and
        period are filtered locally. Since the list is sorted descending, loading stops once
        ``date_from`` is passed. ``server_filter=True`` additionally sends the filters to the
        server (format not confirmed – falls back to ``{}`` on ``MessageNotReadable``).
        """
        body: dict[str, Any] = {}
        if server_filter:
            if account_types:
                body["accountTypes"] = account_types
            if tax_types:
                body["incomeTypes"] = tax_types
            if tickers:
                body["tickers"] = tickers
        lo, hi = _bound(date_from, end=False), _bound(date_to, end=True)
        path = "/rest/reports/v1/income-taxes/tax-transactions/search"
        params: dict[str, Any] = {"limit": page_size}
        while True:
            try:
                page = self._post(path, body=body, params=params)  # type: ignore[attr-defined]
            except ApiError as exc:
                if body and exc.code == "MessageNotReadable":
                    body = {}
                    continue
                raise
            for raw in page:
                t = TaxTransaction.from_json(raw)
                when = _parse_dt(t.time_created)
                if lo and when and when.astimezone() < lo:
                    return                                   # sorted descending → done
                if hi and when and when.astimezone() > hi:
                    continue
                if tax_types and t.tax_type not in tax_types:
                    continue
                if tickers and t.ticker not in tickers:
                    continue
                if account_types and t.account_type not in account_types:
                    continue
                yield t
            if len(page) < page_size:
                return
            params = {"limit": page_size, "idCursor": page[-1]["taxId"],
                      "dateTimeCursor": page[-1]["timeCreated"]}

    def get_tax_transaction_details(self, tx: TaxTransaction | str, tax_type: str = "EQUITY_SOLD",
                                    account_type: str = "EQUITY") -> HistoryDetails:
        if isinstance(tx, TaxTransaction):
            income_id, tax_type, account_type = tx.income_id, tx.tax_type, tx.account_type
        else:
            income_id = tx
        return HistoryDetails.from_json(self._get(  # type: ignore[attr-defined]
            f"/rest/reports/v1/income-taxes/tax-transactions/{account_type}/{tax_type}/{income_id}"))

    def realized_gains(self, *, year: int | None = None, date_from=None, date_to=None,
                       tickers: list[str] | None = None) -> list[RealizedGain]:
        """Realised profit/loss per sale (from the tax transactions + details).

        Loads the details for every sale – with many sales this takes a while.
        """
        if year:
            date_from = date_from or date(year, 1, 1)
            date_to = date_to or date(year, 12, 31)
        out = []
        for tx in self.iter_tax_transactions(tax_types=["EQUITY_SOLD"], tickers=tickers,
                                             date_from=date_from, date_to=date_to):
            det = self.get_tax_transaction_details(tx)
            gains = {c: det.proceeds[c] + det.cost_basis[c] for c in det.proceeds if c in det.cost_basis}
            out.append(RealizedGain(tx.time_created, tx.ticker or det.ticker or "", tx.quantity,
                                    det.proceeds, det.cost_basis, gains, det.return_percent, det.taxes,
                                    tx.income_id))
        return out

    # -------------------------------------------------- positions
    def get_positions(self) -> list[Position]:
        """Open positions with unrealised profit/loss (from the account summary)."""
        summary = self.get_account_summary()  # type: ignore[attr-defined]
        acc = summary.get("accountsByType", {}).get(self.trading_type, {})  # type: ignore[attr-defined]
        return [Position.from_json(p) for p in acc.get("open", [])]

    def _dealer(self) -> str:
        lr = getattr(self, "login_result", None)
        if lr and lr.raw.get("account", {}).get("accountDealer"):
            return lr.raw["account"]["accountDealer"]
        return "T212DE"
