"""Command line interface.

    python -m trading212 [global options] COMMAND [options]

Commands: login, logout, keepalive, budget, accounts, summary, search, price,
          orders, order, cancel, positions, history, details, taxes, tax-transactions, gains
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import Config, Trading212Client, Trading212Error
from .config import load_profile_config, profile_dir
from .exceptions import AuthenticationError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading212", description="Trading212 web API")
    p.add_argument("-P", "--profile", help="separate login (own session, browser profile and optional "
                   "config in ~/.config/trading212/PROFILE/); default: $T212_PROFILE")
    p.add_argument("-c", "--config", help="path to the config file (TOML). "
                   "Default: PROFILE/config.toml, $T212_CONFIG or ~/.config/trading212/config.toml")
    p.add_argument("-u", "--username", help="e-mail (only use a session file of this user)")
    p.add_argument("--session-file", help="path of the session file")
    p.add_argument("--no-session", action="store_true", help="do not read/write a session file")
    p.add_argument("--currency", help="target currency for the account summary, e.g. EUR")
    p.add_argument("--browser-channel", help="login: 'chrome', 'msedge' or 'chromium'")
    p.add_argument("--browser-profile", help="login: browser profile folder ('none' = temporary)")
    p.add_argument("--browser-timeout", type=int, default=300, metavar="S",
                   help="login: max. seconds to wait for the login in the browser (default 300)")
    acc = p.add_mutually_exclusive_group()
    acc.add_argument("--demo", dest="account", action="store_const", const="demo",
                     help="use the demo account (practice money)")
    acc.add_argument("--live", dest="account", action="store_const", const="live",
                     help="use the live account (real money) – default unless the config says otherwise")
    p.add_argument("--json", action="store_true", help="output as JSON")
    p.add_argument("-v", "--verbose", action="store_true")

    # allow common options AFTER the command as well (e.g. "history --json --demo")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="output as JSON")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    cacc = common.add_mutually_exclusive_group()
    cacc.add_argument("--demo", dest="account", action="store_const", const="demo", default=argparse.SUPPRESS,
                      help="demo account (practice money)")
    cacc.add_argument("--live", dest="account", action="store_const", const="live", default=argparse.SUPPRESS,
                      help="live account (real money)")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    _orig_add = sub.add_parser

    def add_parser(name, **kw):
        kw.setdefault("parents", [common])
        return _orig_add(name, **kw)
    sub.add_parser = add_parser  # type: ignore[method-assign]
    for name, hlp in [("login", "log in through a browser window (incl. 2FA), stores the session"),
                      ("logout", "delete the local session"),
                      ("keepalive", "check/renew the session (for cron), exit 3 = login needed"),
                      ("budget", "show budget/cash (default)"),
                      ("accounts", "show accounts"),
                      ("summary", "raw data: cash, orders, positions"),
                      ("orders", "show open orders")]:
        sub.add_parser(name, help=hlp)

    s = sub.add_parser("search", help="search instruments (name, symbol, ISIN)")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=10)

    pr = sub.add_parser("price", help="last price")
    pr.add_argument("symbols", nargs="+", help="ticker (INTC_US_EQ) or symbol/name")
    pr.add_argument("--exchange", "--market", dest="market", help="exchange, e.g. xetra")

    o = sub.add_parser("order", help="place an order",
                       description="Example day limit order: order buy INTC 2 --limit 60")
    o.add_argument("side", choices=["buy", "sell"])
    o.add_argument("symbol", help="ticker (INTC_US_EQ), symbol (INTC), name or ISIN")
    o.add_argument("amount", type=Decimal, help="quantity (or amount with --value)")
    o.add_argument("--limit", type=Decimal, help="limit price → LIMIT or STOP_LIMIT")
    o.add_argument("--stop", type=Decimal, help="stop price → STOP or STOP_LIMIT")
    g = o.add_mutually_exclusive_group()
    g.add_argument("--day", dest="validity", action="store_const", const="DAY",
                   help="valid until end of day (default)")
    g.add_argument("--gtc", dest="validity", action="store_const", const="GOOD_TILL_CANCEL",
                   help="good till cancelled")
    o.add_argument("--extended", action="store_true", help="allow trading in extended hours")
    o.add_argument("--value", action="store_true", help="amount is a value (market order by value)")
    o.add_argument("--currency-settle", "--settle", "--order-currency", dest="order_currency",
                   help="settlement currency/wallet: default = trading currency (US exchange → USD, "
                        "Xetra → EUR); e.g. EUR to convert")
    o.add_argument("--exchange", "--market", dest="market",
                   help="exchange: us (default, NASDAQ/NYSE), nasdaq, nyse, xetra, lse, paris, ... "
                        "– or pass an exact ticker such as INTC_US_EQ / INLd_EQ")
    o.add_argument("--dry-run", action="store_true", help="preview + validation only, send nothing")
    o.add_argument("-y", "--yes", action="store_true", help="send without confirmation")
    o.add_argument("--confirm", action="store_true", help="force confirmation (even if disabled in the config)")

    sub.add_parser("positions", help="open positions with unrealised profit/loss")

    h = sub.add_parser("history", help="history (orders, dividends, transactions, interest)")
    h.add_argument("kind", nargs="?", default="orders", choices=["all", "orders", "dividends",
                                                                  "transactions", "interest"])
    h.add_argument("--symbol", help="only this instrument (symbol, name or ticker)")
    h.add_argument("--exchange", "--market", dest="market", help="exchange for the symbol")
    h.add_argument("-n", "--limit", type=int, default=30, help="max. entries (default 30, 0 = all)")
    h.add_argument("--since", help="only from this date, e.g. 2026-10-01")
    h.add_argument("--details", action="store_true", help="load fill price / P/L for every order")

    dt = sub.add_parser("details", help="details of a history entry (e.g. order id)")
    dt.add_argument("ref", help="id (99319640949) or path (/orders/99319640949, /dividends/…)")
    dt.add_argument("--kind", default="orders", help="type, if only an id is given")
    dt.add_argument("--pdf", metavar="FILE", help="save the trade confirmation as PDF")

    sub.add_parser("taxes", help="tax overview: exemption order, loss pots, result")

    tt = sub.add_parser("tax-transactions", help="tax-relevant transactions")
    tt.add_argument("--year", type=int)
    tt.add_argument("--type", dest="tax_type", help="e.g. EQUITY_SOLD, INTEREST_ON_CASH, DIVIDEND")
    tt.add_argument("--symbol")
    tt.add_argument("-n", "--limit", type=int, default=50)

    gn = sub.add_parser("gains", help="realised profit/loss per sale (+ unrealised)")
    gn.add_argument("--year", type=int, help="tax year (default: current year)")
    gn.add_argument("--symbol")
    gn.add_argument("--currency", dest="gain_currency", default="EUR",
                    help="only sales with amounts in this currency, evaluated in it "
                         "(default EUR = all sales, relevant for German tax)")

    c = sub.add_parser("cancel", help="cancel order(s)",
                       description="Examples: cancel 51234567892 | cancel 1 2 3 | cancel --all "
                                   "| cancel --symbol INTC --side buy")
    c.add_argument("order_ids", nargs="*", help="order id(s), see 'orders'")
    c.add_argument("--all", action="store_true", help="cancel all open orders")
    c.add_argument("--symbol", help="only orders of this ticker/symbol (e.g. INTC or INTC_US_EQ)")
    c.add_argument("--exchange", "--market", dest="market", help="exchange for the symbol, e.g. xetra")
    c.add_argument("--side", choices=["buy", "sell"], help="only buy or sell orders")
    c.add_argument("--no-wait", action="store_true", help="do not wait until the cancellation is complete")
    return p


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items() if k != "raw"}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items() if k != "raw"}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def _dump(obj: Any) -> None:
    print(json.dumps(_to_jsonable(obj), indent=2, ensure_ascii=False))


def _fmt(v: Decimal | None, cur: str = "", places: int = 2) -> str:
    if v is None:
        return "-"
    s = f"{v:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {cur}".rstrip()


def _price(v: Decimal, cur: str = "") -> str:
    """Price with at least 2, at most 6 decimals, formatted as 1.234,56."""
    v = Decimal(v)
    places = max(2, min(6, -v.normalize().as_tuple().exponent))
    return _fmt(v, cur, places)


def _qty(v: Decimal | None) -> str:
    if v is None:
        return "-"
    s = format(v.normalize(), "f")
    return s.replace(".", ",")


def _print_budget(b) -> None:
    c = b.cash
    print(f"Account {b.account_id} ({c.currency})")
    rows = [
        ("Total value", c.total),
        ("Free to invest", c.free_for_stocks),
        ("Spending balance (card)", c.spending_pot),
        ("Blocked by orders", c.blocked_for_stocks),
        ("Cash in pies", c.pie_cash),
        ("Invested", c.stock_investment),
        ("Unrealised P/L", c.ppl),
        ("Realised P/L", c.result),
        ("Net deposits", c.net_deposit),
    ]
    for label, val in rows:
        print(f"  {label:<26}{_fmt(val, c.currency):>18}")
    print("Wallets:")
    for w in b.wallets:
        extra = ""
        if w.converted_free_for_stocks is not None:
            extra = f"  (≈ {_fmt(w.converted_free_for_stocks, w.converted_currency or '')})"
        star = "*" if w.primary else " "
        print(f" {star}{w.currency:<4} free {_fmt(w.free_for_stocks):>14}{extra}")


def _print_orders(orders) -> None:
    if not orders:
        print("No open orders.")
        return
    print(f"{'Order ID':<13}{'Ticker':<14}{'Side':<6}{'Type':<11}{'Qty':>10}{'Limit':>11}{'Stop':>11}"
          f"  {'Cur.':<6}{'Valid':<8}Status")
    for o in orders:
        amount = _qty(abs(o.quantity)) if o.quantity is not None else f"{_fmt(abs(o.value))} (value)"
        validity = "Day" if o.time_validity == "DAY" else "GTC"
        print(f"{o.order_id:<13}{o.ticker:<14}{o.side:<6}{o.type:<11}{amount:>10}"
              f"{_price(o.limit_price) if o.limit_price is not None else '-':>11}"
              f"{_price(o.stop_price) if o.stop_price is not None else '-':>11}"
              f"  {o.currency:<6}{validity:<8}{o.status}")


def _resolve(t212: Trading212Client, symbol: str, market: str | None = None):
    """Return (ticker, Instrument). An exact ticker is looked up too (for its currency)."""
    inst = t212.resolve_ticker(symbol, market=market)
    return inst.ticker, inst


def _cmd_order(t212: Trading212Client, args) -> int:
    ticker, inst = _resolve(t212, args.symbol, args.market)
    currency = t212.settlement_currency(inst, args.order_currency)      # settlement / wallet
    price_cur = (inst.currency or currency).upper()                      # quote currency (limit/stop)
    if price_cur == "GBX":
        price_cur = "GBX (pence)"
    validity = args.validity or "DAY"
    side = args.side.upper()
    name = f"{inst.full_name or inst.name} ({ticker})" if inst else ticker

    if args.value and (args.limit or args.stop):
        print("--value is only possible as a market order (without --limit/--stop).", file=sys.stderr)
        return 2
    if args.value:
        order_type = "MARKET"
    elif args.limit is not None and args.stop is not None:
        order_type = "STOP_LIMIT"
    elif args.limit is not None:
        order_type = "LIMIT"
    elif args.stop is not None:
        order_type = "STOP"
    else:
        order_type = "MARKET"

    side_txt = ("BUY" if side == "BUY" else "SELL") + ("  [DEMO]" if t212.is_demo else "  [LIVE]")
    print(f"{side_txt}  {name}  [{inst.exchange or '-'}, quoted in {price_cur}]")
    native = t212.settlement_currency(inst, "instrument")
    conv = "" if currency == native else "  ⚠ with currency conversion (fee)"
    print(f"  Settlement: {currency} ({'proceeds to' if side == 'SELL' else 'paid from'} {currency} wallet){conv}")
    print(f"  Type:       {order_type}  ({'day order' if validity == 'DAY' else 'good till cancelled'}"
          f"{', extended hours' if args.extended else ''})")
    if args.limit is not None:
        print(f"  Limit:      {_price(args.limit, price_cur)}")
    if args.stop is not None:
        print(f"  Stop:       {_price(args.stop, price_cur)}")

    # like the web client: load the instrument settings first (limits, minimum quantity)
    try:
        st = t212.get_instrument_settings(ticker, currency, order_type)
        if st.suspended:
            print("  Warning: instrument is suspended.")
        if not args.value:
            limit = st.max_buy if side == "BUY" else st.max_sell
            print(f"  max. {'buy' if side == 'BUY' else 'sell'}: {_qty(limit)} shares, "
                  f"minimum {_qty(st.min_trade)}")
    except Trading212Error as exc:
        print(f"  (instrument settings not available: {exc.args[0] if exc.args else exc})")

    body = None
    costs = None
    try:
        if args.value:
            costs = t212.preview_value_order(ticker, args.amount, currency=currency, side=side,
                                             time_validity=validity, extended_hours=args.extended)
        else:
            body = t212.build_order(ticker, args.amount, side=side, order_type=order_type,
                                    currency=currency, limit_price=args.limit, stop_price=args.stop,
                                    time_validity=validity, extended_hours=args.extended)
            costs = t212.preview_order(body)
    except Trading212Error as exc:
        if body is None and not args.value:
            raise
        print(f"  Cost preview not available: {getattr(exc, 'code', None) or exc}")

    if costs is not None:
        print(f"  Quantity:   {_qty(costs.quantity)}"
              + (f"  (for {_fmt(args.amount, currency)})" if args.value else ""))
        print(f"  Order value: {_fmt(costs.order_value, currency)}   "
              f"Total incl. costs: {_fmt(costs.total, currency)}")
        if costs.exchange_rate is not None:
            print(f"  FX rate:    1 {costs.rate_from} = {costs.exchange_rate} {costs.rate_to}")
        if costs.costs:
            names = {"CURRENCY_CONVERSION_FEE": "FX conversion", "TRANSACTION_FEE": "transaction",
                     "FINRA_FEE": "FINRA", "STAMP_DUTY": "stamp duty"}
            parts = [f"{names.get(k, k)} {_fmt(Decimal(str(v)), currency)}" for k, v in costs.costs.items()]
            print(f"  Costs:      {', '.join(parts)}")
    else:
        print(f"  Quantity:   {_qty(args.amount)}" if not args.value else f"  Value:      {_fmt(args.amount, currency)}")

    if body is not None:
        t212.validate_order(body)
        print("  Validation by Trading212: OK")

    if args.dry_run:
        print("Dry run – nothing sent.")
        return 0
    need_confirm = args.confirm or (t212.config.confirm_orders and not args.yes)
    if need_confirm:
        if not sys.stdin.isatty():
            print("Confirmation required but no interactive input – send with -y "
                  "or set [orders] confirm = false.", file=sys.stderr)
            return 1
        answer = input(f"Send order to Trading212 ({t212.sub_system})? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "j", "ja"):
            print("Aborted.")
            return 1

    if args.value:
        order = t212.place_value_order(ticker, args.amount, currency=currency, side=side,
                                       time_validity=validity, extended_hours=args.extended)
    else:
        order = t212.place_order(body, validate=False)   # already validated above
    if args.json:
        _dump(order)
    else:
        print(f"Order created: id {order.order_id}, status {order.status}")
    return 0


def _short_dt(s: str | None) -> str:
    from .history import _parse_dt
    dt = _parse_dt(s)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M") if dt else (s or "")


def _signed(v: Decimal | None, cur: str = "") -> str:
    if v is None:
        return "-"
    return ("+" if v > 0 else "") + _fmt(v, cur)


def _ticker_arg(t212: Trading212Client, symbol: str | None, market: str | None = None) -> str | None:
    if not symbol:
        return None
    return symbol if "_" in symbol else t212.resolve_ticker(symbol, market=market).ticker


def _cmd_positions(t212: Trading212Client, args) -> int:
    pos = t212.get_positions()
    if args.json:
        _dump(pos)
        return 0
    cur = t212.currency
    print(f"{'Ticker':<14}{'Shares':>16}{'Avg price*':>13}{'Invested':>14}{'Value':>14}{'P/L':>13}{'%':>8}")
    tot_inv = tot_val = tot_ppl = Decimal("0")
    for p in sorted(pos, key=lambda x: x.value, reverse=True):
        pct = p.ppl_percent
        qty = _qty(p.quantity.quantize(Decimal("0.000001")).normalize()) if p.quantity else "0"
        print(f"{p.ticker:<14}{qty:>16} {_fmt(p.average_price, places=2):>12}{_fmt(p.investment):>14}"
              f"{_fmt(p.value):>14}{_signed(p.ppl):>13}{(_signed(pct) if pct is not None else '-'):>8}")
        tot_inv += p.investment
        tot_val += p.value
        tot_ppl += p.ppl
    pct = (tot_ppl / tot_inv * 100) if tot_inv else None
    print(f"{'Total':<14}{'':>16}{'':>13}{_fmt(tot_inv):>14}{_fmt(tot_val):>14}{_signed(tot_ppl):>13}"
          f"{(_signed(pct) if pct is not None else '-'):>8}   ({cur}; *avg price in quote currency)")
    return 0


def _cmd_history(t212: Trading212Client, args) -> int:
    ticker = _ticker_arg(t212, args.symbol, args.market)
    limit = None if args.limit == 0 else args.limit
    entries = list(t212.iter_history(args.kind, instrument=ticker, since=args.since, max_items=limit))
    details = {}
    if args.details:
        for e in entries:
            if e.details_type == "orders" and e.status in ("BUY", "SELL"):
                details[e.details_id] = t212.get_history_details(e)
    if args.json:
        out = []
        for e in entries:
            d = _to_jsonable(e)
            if e.details_id in details:
                d["details"] = _to_jsonable(details[e.details_id])
            out.append(d)
        _dump(out)
        return 0
    if not entries:
        print("No entries.")
        return 0
    for e in entries:
        what = e.name or e.category or ""
        qty = f"{_qty(e.quantity)} sh" if e.quantity is not None else ""
        line = (f"{_short_dt(e.date):<17}{e.label:<18}{(e.ticker or ''):<14}{what[:30]:<31}"
                f"{qty:>16}{_signed(e.amount, e.currency or ''):>18}")
        if e.second_amount is not None:
            line += f"  → {_fmt(e.second_amount, e.second_currency or '')}"
        d = details.get(e.details_id)
        if d:
            fill = f"@ {_price(d.fill_price, d.price_currency or '')}" if d.fill_price is not None else ""
            gv = ""
            if d.return_amount is not None and e.status == "SELL":
                g_eur = d.gain("EUR")
                gv = f"  P/L {_signed(d.return_amount, d.return_currency or '')}"
                if g_eur is not None and d.return_currency != "EUR":
                    gv += f" ({_signed(g_eur, 'EUR')})"
            line += f"  {fill}{gv}"
        print(line + (f"   [{e.details_type}/{e.details_id}]" if e.details_id else ""))
    try:
        for sv in t212.get_history_summary(args.kind):
            print(f"{sv.label}: {_signed(sv.amount, sv.currency)}")
    except Trading212Error:
        pass
    return 0


def _print_details(d) -> None:
    print(f"{d.title}  –  {_short_dt(d.date)}")
    if d.amount is not None:
        print(f"  Amount: {_signed(d.amount, d.currency or '')}")
    last = None
    for r in d.rows:
        if r.section and r.section != last and not r.is_total:
            print(f"  [{r.section}]")
        last = r.section
        if r.is_total:
            print(f"  [{r.section}]  {r.text}")
            last = r.section
            continue
        print(f"    {r.label:<22}{r.text}")


def _cmd_details(t212: Trading212Client, args) -> int:
    d = t212.get_history_details(args.ref, kind=args.kind)
    if args.json:
        _dump(d)
    else:
        _print_details(d)
    if args.pdf:
        data = t212.download_trade_confirmation(d)
        Path(args.pdf).write_bytes(data)
        print(f"Trade confirmation saved: {args.pdf} ({len(data)} bytes)")
    return 0


def _cmd_taxes(t212: Trading212Client, args) -> int:
    o = t212.get_tax_overview()
    pots = {}
    for k in ("stock", "other", "withholding"):
        try:
            pots[k] = t212.get_loss_pot(k)
        except Trading212Error:
            pots[k] = None
    try:
        nv = t212.get_nv_status()
    except Trading212Error:
        nv = None
    if args.json:
        _dump({"overview": o, "pots": pots, "nv_status": nv})
        return 0
    print(f"Tax year {o.tax_year}")
    print(f"  Exemption order          {_fmt(o.exemption_used, 'EUR')} of {_fmt(o.exemption_limit, 'EUR')} used"
          f"  → remaining {_fmt(o.exemption_remaining, 'EUR')}" if o.exemption_limit is not None
          else "  Exemption order          none")
    print(f"  Result (return)          {_signed(o.total_return, 'EUR')}")
    print(f"  Losses offset            {_fmt(o.offsets_used, 'EUR')}")
    print(f"  Taxes paid               {_fmt(o.taxes_paid, 'EUR')}")
    print("  Loss pots (current balance)")
    print(f"    Stocks                 {_fmt(o.stock_loss_pot, 'EUR')}")
    print(f"    Other                  {_fmt(o.other_loss_pot, 'EUR')}")
    print(f"    Withholding tax        {_fmt(o.withholding_tax_pot, 'EUR')}")
    names = {"stock": "Stocks", "other": "Other", "withholding": "Withholding tax"}
    for k, pot in pots.items():
        if pot and pot.total_available is not None:
            extra = ""
            if pot.previous_year is not None:
                extra = f" (previous year {_fmt(pot.previous_year)}, current year {_fmt(pot.current_year)})"
            print(f"    {names[k]} pot: accrued {_fmt(pot.total_available, 'EUR')}, offset "
                  f"{_fmt(pot.total_used, 'EUR')}{extra}")
    lc = {"NOT_REQUESTED": "not requested", "REQUESTED": "requested", "INELIGIBLE": "not possible"}
    if o.loss_certificate_status:
        print(f"  Loss certificate         {lc.get(o.loss_certificate_status, o.loss_certificate_status)}"
              f" (deadline {_short_dt(o.loss_certificate_deadline)[:10]})")
    if nv is not None:
        print(f"  NV certificate           {'none' if nv == 'NONE' else nv}")
    if o.tax_id:
        print(f"  Tax ID                   ******{o.tax_id[-4:]}")
    return 0


def _cmd_tax_transactions(t212: Trading212Client, args) -> int:
    from datetime import date as _date
    ticker = _ticker_arg(t212, args.symbol)
    kw = {}
    if args.year:
        kw = {"date_from": _date(args.year, 1, 1), "date_to": _date(args.year, 12, 31)}
    items = []
    for tx in t212.iter_tax_transactions(tax_types=[args.tax_type] if args.tax_type else None,
                                         tickers=[ticker] if ticker else None, **kw):
        items.append(tx)
        if args.limit and len(items) >= args.limit:
            break
    if args.json:
        _dump(items)
        return 0
    for tx in items:
        q = f"{_qty(tx.quantity)} sh" if tx.quantity is not None else ""
        print(f"{_short_dt(tx.time_created):<17}{tx.label:<10}{(tx.ticker or ''):<14}{q:>12}"
              f"   tax {_fmt(tx.tax_amount, tx.currency):>12}   [{tx.tax_type}/{tx.income_id}]")
    print(f"{len(items)} transactions, total taxes {_fmt(sum((t.tax_amount for t in items), Decimal('0')), 'EUR')}")
    return 0


def _cmd_gains(t212: Trading212Client, args) -> int:
    from datetime import date as _date
    year = args.year or _date.today().year
    ticker = _ticker_arg(t212, args.symbol)
    cur = args.gain_currency.upper()
    all_gains = t212.realized_gains(year=year, tickers=[ticker] if ticker else None)
    # EUR sales have no USD amounts etc. – only show sales traded / valued in this currency
    gains = [g for g in all_gains if cur in g.gain]
    skipped = len(all_gains) - len(gains)
    positions = t212.get_positions()
    if args.json:
        _dump({"year": year, "currency": cur, "realized": gains, "positions": positions})
        return 0
    print(f"Realised profit/loss {year} (per sale, in {cur})")
    total = Decimal("0")
    win = loss = Decimal("0")
    for g in sorted(gains, key=lambda x: x.date):
        val = g.gain.get(cur)
        other = [f"{_signed(v, c)}" for c, v in g.gain.items() if c != cur]
        print(f"  {_short_dt(g.date):<17}{g.ticker:<14}{_qty(g.quantity) if g.quantity else '':>8} sh"
              f"  proceeds {_fmt(g.proceeds.get(cur), cur):>14}  cost {_fmt(g.cost_basis.get(cur), cur):>14}"
              f"  P/L {_signed(val, cur):>13}" + (f"  ({', '.join(other)})" if other else ""))
        if val is not None:
            total += val
            if val >= 0:
                win += val
            else:
                loss += val
    print(f"  {len(gains)} sales: gains {_signed(win, cur)}, losses {_signed(loss, cur)}, "
          f"net {_signed(total, cur)}")
    if skipped:
        print(f"  ({skipped} sales without {cur} amounts not shown – e.g. --currency EUR shows all)")
    unreal = sum((p.ppl for p in positions), Decimal("0"))
    print(f"Unrealised (open positions): {_signed(unreal, t212.currency)}")
    try:
        o = t212.get_tax_overview()
        if o.tax_year == year:
            print(f"Tax overview {year}: result {_signed(o.total_return, 'EUR')}, "
                  f"taxes {_fmt(o.taxes_paid, 'EUR')}, stock loss pot {_fmt(o.stock_loss_pot, 'EUR')}")
    except Trading212Error:
        pass
    return 0


def _cmd_cancel(t212: Trading212Client, args) -> int:
    if not args.order_ids and not args.all and not args.symbol and not args.side:
        print("Pass order id(s) or --all / --symbol / --side.", file=sys.stderr)
        return 2
    open_orders = {o.order_id: o for o in t212.get_orders()}

    if args.order_ids:
        targets = []
        for oid in args.order_ids:
            if oid not in open_orders:
                print(f"  {oid}: no open order with this id", file=sys.stderr)
            else:
                targets.append(open_orders[oid])
    else:
        ticker = None
        if args.symbol:
            ticker = args.symbol if "_" in args.symbol else t212.resolve_ticker(args.symbol, market=args.market).ticker
        targets = [o for o in open_orders.values()
                   if (ticker is None or o.ticker.upper() == ticker.upper())
                   and (args.side is None or o.side == args.side.upper())]

    targets = [o for o in targets if o.status != "CANCELLING"]
    if not targets:
        print("Nothing to cancel.")
        return 1 if args.order_ids else 0

    failed = 0
    for o in targets:
        amount = _qty(abs(o.quantity)) if o.quantity is not None else _fmt(abs(o.value), o.currency)
        limit = f" @ {_price(o.limit_price, o.currency)}" if o.limit_price is not None else ""
        label = f"{o.order_id}  {o.side} {amount} {o.ticker} {o.type}{limit}"
        try:
            res = t212.cancel_order(o.order_id, value_order=o.quantity is None, wait=not args.no_wait)
        except Trading212Error as exc:
            failed += 1
            print(f"  ✗ {label}: {exc.args[0] if exc.args else exc}", file=sys.stderr)
            continue
        status = "cancelled" if res is None else f"status {res.status}"
        print(f"  ✓ {label}: {status}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "budget"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        overrides: dict[str, Any] = {
            "username": args.username,
            "session_file": args.session_file,
            "target_currency": args.currency,
        }
        profile = args.profile or os.environ.get("T212_PROFILE")
        browser_profile = args.browser_profile
        if profile:
            browser_profile = browser_profile or str(profile_dir(profile) / "browser-profile")
        cfg, _ = load_profile_config(profile, args.config, **overrides)
        if args.no_session:
            cfg.session_file = None

        with Trading212Client(cfg) as t212:
            if command == "logout":
                t212.logout_local()
                print("Local session deleted.")
                return 0

            if command == "login":
                from .browser_login import browser_login
                if cfg.session_file is None:
                    print("login needs a session file (do not use --no-session).", file=sys.stderr)
                    return 2
                kwargs: dict[str, Any] = {}
                if browser_profile:
                    kwargs["profile_dir"] = (None if browser_profile.lower() == "none"
                                             else Path(browser_profile).expanduser())
                browser_login(t212, channel=args.browser_channel, timeout_s=args.browser_timeout, **kwargs)
                print(f"Session saved to {cfg.session_file} – account {t212.account_id}")
                return 0

            try:
                t212.ensure_login(account=args.account)
            except AuthenticationError as exc:
                login_cmd = "python -m trading212 " + (f"--profile {profile} login" if profile else "login")
                print(f"Not logged in and automatic re-login failed ({exc.error_type or exc.code}). "
                      f"Run '{login_cmd}' (browser window) once and copy {cfg.session_file}.",
                      file=sys.stderr)
                return 3
            if command == "keepalive":
                print(f"Session OK – account {t212.account_id} ({t212.sub_system})")
                return 0
            if not args.json and command not in ("search",):
                tag = "DEMO (practice money)" if t212.is_demo else "LIVE (real money)"
                print(f"[{tag}] account {t212.account_id}", file=sys.stderr)

            if command == "budget":
                b = t212.get_budget()
                _dump(b) if args.json else _print_budget(b)
            elif command == "accounts":
                _dump(t212.get_accounts())
            elif command == "summary":
                _dump(t212.get_account_summary())
            elif command == "orders":
                orders = t212.get_orders()
                _dump(orders) if args.json else _print_orders(orders)
            elif command == "search":
                hits = t212.search_instruments(args.query, limit=args.limit)
                if args.json:
                    _dump(hits)
                else:
                    for i in hits:
                        print(f"{i.ticker:<16}{i.short_name:<8}{i.category:<8}{(i.currency or ''):<5}{(i.exchange or ''):<16}"
                              f"{i.full_name or i.name}")
            elif command == "price":
                tickers = [s if "_" in s else t212.resolve_ticker(s, market=args.market).ticker for s in args.symbols]
                prices = t212.get_last_prices(tickers)
                if args.json:
                    _dump(prices)
                else:
                    for t in tickers:
                        print(f"{t:<16}{prices.get(t, '-')}")
            elif command == "order":
                return _cmd_order(t212, args)
            elif command == "cancel":
                return _cmd_cancel(t212, args)
            elif command == "positions":
                return _cmd_positions(t212, args)
            elif command == "history":
                return _cmd_history(t212, args)
            elif command == "details":
                return _cmd_details(t212, args)
            elif command == "taxes":
                return _cmd_taxes(t212, args)
            elif command == "tax-transactions":
                return _cmd_tax_transactions(t212, args)
            elif command == "gains":
                return _cmd_gains(t212, args)
        return 0
    except Trading212Error as exc:
        print(f"Error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        body = getattr(exc, "body", "")
        if body and (args.verbose or getattr(exc, "error_type", None) is None):
            print(body, file=sys.stderr)
        ctx = getattr(exc, "context", None)
        if ctx and not args.verbose:
            print(json.dumps(ctx, ensure_ascii=False), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
