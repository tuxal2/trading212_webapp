"""Place take-profit sell orders: every position at its average buy price + 1 %.

For each position of the chosen market (US stocks in USD or EUR stocks) a limit sell
order for the whole position is placed at ``average price × (1 + percent/100)``, valid
until cancelled. Existing sell orders of that stock are cancelled first; an existing
order with the same quantity and limit is kept as it is.

    [sellprofit]
    percent = 1                   # profit over the average buy price
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from trading212 import Trading212Client, Trading212Error
from trading212.models import Order

from .common import (account_label, add_common_args, confirm, err, fmt, load_section, login,
                     open_client, setup_logging)

MARKETS = {"us": "USD", "eur": "EUR"}
TIME_VALIDITY = "GOOD_TILL_CANCEL"


@dataclass
class Planned:
    ticker: str
    currency: str = ""
    quantity: Decimal = Decimal(0)
    average_price: Decimal = Decimal(0)
    limit: Decimal = Decimal(0)
    cancel: list[Order] = field(default_factory=list)   # existing sell orders to cancel
    body: dict = field(default_factory=dict)
    problem: str = ""                                   # reason the position is skipped
    keep: bool = False                                  # an identical order exists already
    note: str = ""
    in_pie: bool = False                                # whole position in a pie → not shown
    result: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem and not self.keep and not self.in_pie


def limit_price(average: Decimal, percent: Decimal) -> Decimal:
    """Average price + percent, rounded up (2 decimals, 4 below 1)."""
    places = Decimal("0.0001") if average < 1 else Decimal("0.01")
    return (average * (1 + percent / 100)).quantize(places, rounding=ROUND_CEILING)


def _open_qty(o: Order) -> Decimal:
    return abs(o.quantity or 0) - abs(o.filled_quantity or 0)


def plan(t212: Trading212Client, currency: str | None, percent: Decimal) -> list[Planned]:
    """Positions of ``currency`` (None = all) → planned sell orders; nothing is changed."""
    sells: dict[str, list[Order]] = {}
    for o in t212.get_orders():
        if o.side == "SELL":
            sells.setdefault(o.ticker, []).append(o)

    out = []
    for pos in sorted(t212.get_positions(), key=lambda p: p.ticker):
        p = Planned(pos.ticker, average_price=pos.average_price)
        cancel = sells.get(pos.ticker, [])
        if any(o.quantity is None for o in cancel):      # value orders: amount unknown in shares
            p.quantity = pos.quantity
        else:
            free = pos.sellable if pos.sellable is not None else pos.quantity
            p.quantity = min(pos.quantity, free + sum(_open_qty(o) for o in cancel))
        pie = pos.autoinvest_quantity or Decimal(0)
        if p.quantity <= 0 and pie > 0:
            p.in_pie = True                              # pie shares cannot be sold by order
            if not currency or ("_US_" in pos.ticker) == (currency == "USD"):   # rough market, display only
                out.append(p)
            continue
        if pie > 0:
            p.note = f"{pie.normalize():f} sh in a pie stay"
        try:
            inst = t212.resolve_ticker(pos.ticker)
        except Trading212Error as exc:
            p.problem = f"instrument not found: {err(exc)}"
            out.append(p)
            continue
        p.currency = (inst.currency or "").upper()
        if currency and p.currency != currency:
            continue                                     # other market – not shown
        out.append(p)
        if pos.average_price <= 0:
            p.problem = "no average buy price"
            continue
        p.cancel = cancel
        if p.quantity <= 0:
            p.problem = "nothing sellable"
            continue
        p.limit = limit_price(pos.average_price, percent)
        old = p.cancel[0] if len(p.cancel) == 1 else None
        if (old and old.type == "LIMIT" and old.limit_price == p.limit and _open_qty(old) == p.quantity
                and old.time_validity == TIME_VALIDITY):
            p.keep = True
            p.cancel = []
            continue
        p.body = t212.build_order(pos.ticker, p.quantity, side="SELL", order_type="LIMIT",
                                  currency=t212.settlement_currency(inst), limit_price=p.limit,
                                  time_validity=TIME_VALIDITY)
        if not p.cancel:                 # with existing sell orders the shares are still blocked
            try:
                t212.validate_order(p.body)
            except Trading212Error as exc:
                p.problem = f"rejected: {err(exc)}"
    return out


def execute(t212: Trading212Client, planned: list[Planned]) -> int:
    """Cancel the old sell orders, place the new ones; returns the number of failures."""
    failed = 0
    for p in planned:
        if not p.ok:
            continue
        try:
            for o in p.cancel:
                left = t212.cancel_order(o.order_id, value_order=o.quantity is None, wait=True)
                if left is not None and left.status not in ("CANCELLED", "REJECTED"):
                    raise Trading212Error(f"old order {o.order_id} still {left.status}")
        except Trading212Error as exc:
            failed += 1
            p.result = f"FAILED to cancel the old sell order: {err(exc)} – nothing new placed"
            continue
        try:
            order = t212.place_order(p.body)
            p.result = f"sent, order {order.order_id} ({order.status})"
        except Trading212Error as exc:
            failed += 1
            p.result = (f"FAILED: {err(exc)}"
                        + (" – the old sell order IS CANCELLED" if p.cancel else ""))
    return failed


def print_plan(planned: list[Planned], account: str, percent: Decimal) -> None:
    print(f"[{account}]  limit sell orders at average buy price +{percent.normalize():f} %, "
          f"valid until cancelled")
    print(f"  {'Ticker':<14}{'Qty':>12}  {'Avg. price':>14}  {'Limit':>14}  Status")
    in_pie = [p for p in planned if p.in_pie]
    planned = [p for p in planned if not p.in_pie]
    for p in planned:
        if p.result:
            status = p.result
        elif p.problem:
            status = f"skipped: {p.problem}"
        elif p.keep:
            status = "unchanged (same sell order exists)"
        else:
            status = "ok" + (f", cancels {len(p.cancel)} sell order(s) first" if p.cancel else "")
        if p.note:
            status += f" ({p.note})"
        qty = f"{p.quantity.normalize():f}" if p.quantity else ""
        print(f"  {p.ticker:<14}{qty:>12}  {fmt(p.average_price, p.currency):>14}  "
              f"{fmt(p.limit, p.currency, 4 if p.limit and p.limit < 1 else 2) if p.limit else '':>14}  "
              f"{status}")
    print(f"  {sum(p.ok for p in planned)} of {len(planned)} positions get a new sell order, "
          f"{sum(len(p.cancel) for p in planned if p.ok)} old sell order(s) to cancel")
    if in_pie:
        print(f"  {len(in_pie)} positions held only in pies (AutoInvest) – cannot be sold by order: "
              + ", ".join(p.ticker for p in in_pie))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="t212-sellprofit",
        description="Limit sell order for every position at average buy price + 1 %% (whole "
                    "position, valid until cancelled). Existing sell orders are cancelled first.")
    p.add_argument("market", choices=["us", "eur", "all"],
                   help="us = stocks in USD, eur = stocks in EUR, all = both")
    p.add_argument("--percent", type=Decimal, help="profit in %% (default: [sellprofit] percent or 1)")
    add_common_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        t212, config_file = open_client(args)
        settings = load_section(config_file, "sellprofit")
        percent = args.percent if args.percent is not None else Decimal(str(settings.get("percent", 1)))
        if percent <= 0:
            print("percent must be > 0", file=sys.stderr)
            return 2
        with t212:
            if not login(t212, args):
                return 3
            account = account_label(t212)
            planned = plan(t212, MARKETS.get(args.market), percent)
            print_plan(planned, account, percent)
            ready = [p for p in planned if p.ok]
            if args.dry_run or not ready:
                return 0
            n_cancel = sum(len(p.cancel) for p in ready)
            stop = confirm(args, f"Place {len(ready)} sell orders"
                                 + (f" and cancel {n_cancel} old ones" if n_cancel else "")
                                 + f" in {account}?")
            if stop is not None:
                return stop
            failed = execute(t212, planned)
            print()
            print_plan(planned, account, percent)
            return 1 if failed else 0
    except Trading212Error as exc:
        print(f"Error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
