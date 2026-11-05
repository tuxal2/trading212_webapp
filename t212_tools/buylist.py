"""Buy a list of stocks (copied from a website) with Trading212 limit orders.

Input, one line per stock, tab separated (as copied from the website)::

    TEAM  Atlassian Corp Plc  US0494681010  USD  181.80  181.80  183.80  MOUNT US Tech 100, ...

Per line: symbol, name, ISIN, currency, three prices, strategies. The first price is the
limit. The quantity is ``floor(amount / limit)`` whole shares, the amount per stock comes
from the config::

    [buylist]
    amount = 1000                 # per stock, in the stock's currency
    # amount = { USD = 1000, EUR = 900 }
    skip_existing = true          # skip stocks that already have an open buy order
    skip_positions = false        # also skip stocks you already hold

Every order is validated by Trading212 and shown with its costs before anything is sent.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from trading212 import Trading212Client, Trading212Error
from trading212.exceptions import ApiError

from .common import (account_label, add_common_args, confirm, err as _err, fmt as _fmt, load_section,
                     login, open_client, setup_logging)

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
TIME_VALIDITY = "DAY"


@dataclass
class Row:
    symbol: str
    name: str
    isin: str
    currency: str
    prices: list[Decimal]
    strategies: str
    line_no: int

    @property
    def limit(self) -> Decimal:
        return self.prices[0]


@dataclass
class Planned:
    row: Row
    ticker: str = ""
    quantity: int = 0
    settle_currency: str = ""
    total: Decimal | None = None          # incl. costs, in settlement currency
    body: dict = field(default_factory=dict)
    problem: str = ""                     # reason the line is skipped
    note: str = ""                        # information only (position, open sell order)
    result: str = ""                      # after sending

    @property
    def ok(self) -> bool:
        return not self.problem


# ------------------------------------------------------------------ parsing
def _num(s: str) -> Decimal:
    s = s.strip().replace(" ", "").replace(" ", "")
    if "," in s and "." in s:                 # 1,234.56 or 1.234,56
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return Decimal(s)


def parse_list(text: str) -> tuple[list[Row], list[str]]:
    """Parse the copied list → (rows, warnings). Header and empty lines are ignored."""
    rows: list[Row] = []
    warnings: list[str] = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        cols = [c.strip() for c in (line.split("\t") if "\t" in line else re.split(r"\s{2,}", line))]
        isin_at = next((i for i, c in enumerate(cols) if ISIN_RE.match(c.upper())), None)
        if isin_at is None or isin_at < 1 or len(cols) < isin_at + 3:
            if no > 1 or any(ch.isdigit() for ch in line):
                warnings.append(f"line {no}: not understood – ignored: {line[:60]}")
            continue
        prices = []
        for c in cols[isin_at + 2:isin_at + 5]:
            try:
                prices.append(_num(c))
            except InvalidOperation:
                break
        if not prices or prices[0] <= 0:
            warnings.append(f"line {no}: no valid price – ignored: {line[:60]}")
            continue
        rows.append(Row(symbol=cols[0].upper(), name=" ".join(cols[1:isin_at]), isin=cols[isin_at].upper(),
                        currency=cols[isin_at + 1].upper(), prices=prices,
                        strategies=" ".join(cols[isin_at + 2 + len(prices):]), line_no=no))
    seen: set[str] = set()
    for r in list(rows):
        if r.isin in seen:
            warnings.append(f"line {r.line_no}: {r.symbol} appears twice – only the first is used")
            rows.remove(r)
        seen.add(r.isin)
    return rows, warnings


# ------------------------------------------------------------------ config
def amount_for(settings: dict, currency: str, override: Decimal | None) -> Decimal | None:
    if override is not None:
        return override
    amount = settings.get("amount")
    if isinstance(amount, dict):
        amount = {k.upper(): v for k, v in amount.items()}.get(currency.upper())
    return Decimal(str(amount)) if amount not in (None, "", 0) else None


def quantity_for(amount: Decimal, limit: Decimal) -> int:
    """Whole shares that fit into ``amount`` at ``limit`` (no fractions)."""
    return int((amount / limit).to_integral_value(rounding=ROUND_FLOOR))


# ------------------------------------------------------------------ planning
def plan(t212: Trading212Client, rows: list[Row], settings: dict,
         amount_override: Decimal | None = None) -> list[Planned]:
    """Resolve, size and validate every line – nothing is sent."""
    skip_existing = settings.get("skip_existing", True)
    skip_positions = settings.get("skip_positions", False)
    orders = t212.get_orders()
    open_buys = {o.ticker for o in orders if o.side == "BUY"}
    open_sells = {o.ticker for o in orders if o.side == "SELL"}
    held = {p.ticker: p.quantity for p in t212.get_positions()}

    out = []
    for row in rows:
        p = Planned(row)
        out.append(p)
        amount = amount_for(settings, row.currency, amount_override)
        if amount is None:
            p.problem = f"no amount for {row.currency} ([buylist] amount)"
            continue
        try:
            inst = t212.resolve_ticker(row.isin, currency=row.currency)
        except Trading212Error as exc:
            p.problem = f"not found: {_err(exc)}"
            continue
        p.ticker = inst.ticker
        if inst.isin and inst.isin.upper() != row.isin:
            p.problem = f"ISIN mismatch ({inst.isin})"
            continue
        if inst.currency and inst.currency.upper() != row.currency:
            p.problem = f"listing in {inst.currency}, list says {row.currency}"
            continue
        notes = []
        if inst.ticker in held:
            notes.append(f"holding {held[inst.ticker].normalize():f} sh")
        if inst.ticker in open_sells:
            notes.append("open sell order")
        p.note = ", ".join(notes)
        if skip_existing and inst.ticker in open_buys:
            p.problem = "open buy order exists"
            continue
        if skip_positions and inst.ticker in held:
            p.problem = "position held (skip_positions)"
            continue
        p.quantity = quantity_for(amount, row.limit)
        if p.quantity < 1:
            p.problem = f"amount {amount} {row.currency} < 1 share at {row.limit}"
            continue
        p.settle_currency = t212.settlement_currency(inst)
        p.body = t212.build_order(inst.ticker, p.quantity, side="BUY", order_type="LIMIT",
                                  currency=p.settle_currency, limit_price=row.limit,
                                  time_validity=TIME_VALIDITY)
        try:
            t212.validate_order(p.body)
            p.total = t212.preview_order(p.body).total
        except ApiError as exc:
            p.problem = f"rejected: {_err(exc)}"
    return out


def send(t212: Trading212Client, planned: list[Planned]) -> int:
    """Place the valid orders; returns the number of failures."""
    failed = 0
    for p in planned:
        if not p.ok:
            continue
        try:
            order = t212.place_order(p.body, validate=False)
            p.result = f"sent, order {order.order_id} ({order.status})"
        except Trading212Error as exc:
            failed += 1
            p.result = f"FAILED: {_err(exc)}"
    return failed


# ------------------------------------------------------------------ output
def print_plan(planned: list[Planned], account: str) -> None:
    print(f"[{account}]  limit buy orders, valid {TIME_VALIDITY}")
    print(f"  {'Symbol':<7}{'Ticker':<14}{'Qty':>5}  {'Limit':>12}  {'Total incl. costs':>20}  Status")
    totals: dict[str, Decimal] = {}
    for p in planned:
        r = p.row
        status = p.result or (p.problem and f"skipped: {p.problem}") or "ok"
        if p.note:
            status += f" ({p.note})"
        print(f"  {r.symbol:<7}{p.ticker or '-':<14}{p.quantity or '':>5}  {_fmt(r.limit, r.currency):>12}  "
              f"{_fmt(p.total, p.settle_currency) if p.ok else '':>20}  {status}")
        if p.ok and p.total is not None:
            totals[p.settle_currency] = totals.get(p.settle_currency, Decimal(0)) + p.total
    n = sum(p.ok for p in planned)
    print(f"  {n} of {len(planned)} orders ready, total "
          + (", ".join(_fmt(v, c) for c, v in totals.items()) or "0"))


# ------------------------------------------------------------------ main
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="t212-buylist",
        description="Buy a stock list copied from a website with Trading212 limit orders "
                    "(limit = first price, whole shares, valid until end of day).")
    p.add_argument("file", nargs="?", help="file with the list ('-' or omitted = paste / stdin)")
    p.add_argument("--amount", type=Decimal, help="amount per stock, overrides [buylist] amount")
    add_common_args(p)
    return p


def _read_input(path: str | None) -> str:
    if path and path != "-":
        return Path(path).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        print("Paste the list, then press Ctrl-D (Windows: Ctrl-Z, Enter):", file=sys.stderr)
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        rows, warnings = parse_list(_read_input(args.file))
        for w in warnings:
            print(f"Warning: {w}", file=sys.stderr)
        if not rows:
            print("No stocks found in the input.", file=sys.stderr)
            return 2

        t212, config_file = open_client(args)
        settings = load_section(config_file, "buylist")
        with t212:
            if not login(t212, args):
                return 3
            account = account_label(t212)
            planned = plan(t212, rows, settings, args.amount)
            print_plan(planned, account)
            ready = [p for p in planned if p.ok]
            if args.dry_run or not ready:
                return 0 if ready or args.dry_run else 1
            stop = confirm(args, f"Send {len(ready)} orders to {account}?")
            if stop is not None:
                return stop
            failed = send(t212, planned)
            print()
            print_plan(planned, account)
            return 1 if failed else 0
    except Trading212Error as exc:
        print(f"Error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
