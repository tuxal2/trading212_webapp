"""Offline test of t212-sellprofit: limits, quantities, cancelling, keeping (no network).

    python tests/test_sellprofit.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t212_tools.sellprofit import execute, limit_price, plan  # noqa: E402
from trading212 import Trading212Client  # noqa: E402
from trading212.exceptions import ApiError  # noqa: E402
from trading212.models import Order  # noqa: E402

D = Decimal


def pos(ticker, qty, avg, sellable=None, pie="0"):
    return SimpleNamespace(ticker=ticker, quantity=D(qty), average_price=D(avg),
                           sellable=D(sellable) if sellable is not None else D(qty),
                           autoinvest_quantity=D(pie))


def sell(oid, ticker, qty, limit, validity="GOOD_TILL_CANCEL", filled="0"):
    return Order(order_id=oid, ticker=ticker, type="LIMIT", side="SELL", quantity=-D(qty),
                 filled_quantity=D(filled), value=None, limit_price=D(limit), stop_price=None,
                 currency="USD", status="NEW", time_validity=validity, extended_hours=False,
                 created=None, frontend=None)


class FakeT212:
    CURRENCY = {"INTC_US_EQ": "USD", "ALNY_US_EQ": "USD", "NVDA_US_EQ": "USD", "AAPL_US_EQ": "USD",
                "BAYNd_EQ": "EUR", "PENNY_US_EQ": "USD"}

    def __init__(self):
        self.cancelled, self.placed, self.validated = [], [], []
        self.orders = [sell("o1", "ALNY_US_EQ", "4", "160.00"),
                       sell("o2", "NVDA_US_EQ", "10", "181.80"),            # = target → keep
                       SimpleNamespace(ticker="INTC_US_EQ", side="BUY")]
        self.positions = [pos("INTC_US_EQ", "19.5", "50.00"),
                          pos("ALNY_US_EQ", "10", "151.70", sellable="6"),  # 4 blocked by o1
                          pos("NVDA_US_EQ", "10", "180.00", sellable="0"),
                          pos("BAYNd_EQ", "100", "25.10"),
                          pos("PENNY_US_EQ", "1000", "0.5123"),
                          pos("PIE_US_EQ", "2.5", "10", sellable="0", pie="2.5"),   # only in a pie
                          pos("AAPL_US_EQ", "3.5", "200", sellable="3", pie="0.5")]

    def get_orders(self):
        return self.orders

    def get_positions(self):
        return self.positions

    def resolve_ticker(self, ticker):
        assert ticker != "PIE_US_EQ"                   # pie-only positions are never looked up
        return SimpleNamespace(ticker=ticker, currency=self.CURRENCY[ticker])

    def settlement_currency(self, inst):
        return inst.currency

    build_order = staticmethod(Trading212Client.build_order)

    def validate_order(self, body):
        self.validated.append(body["instrumentCode"])

    def cancel_order(self, oid, value_order=None, wait=False):
        self.cancelled.append(oid)
        return None

    def place_order(self, body, validate=True):
        if body["instrumentCode"] == "INTC_US_EQ":
            raise ApiError(400, "BusinessException", "MarketClosed", {}, "", "")
        self.placed.append(body)
        return SimpleNamespace(order_id=f"n{len(self.placed)}", status="NEW")


def test_limit_price():
    assert limit_price(D("151.70"), D(1)) == D("153.22")          # 153.217 → rounded up
    assert limit_price(D("100"), D(1)) == D("101.00")
    assert limit_price(D("0.5123"), D(1)) == D("0.5175")          # 4 decimals below 1
    assert limit_price(D("180.00"), D("1")) == D("181.80")


def test_plan_us_and_execute():
    t = FakeT212()
    planned = plan(t, "USD", D(1))
    by = {p.ticker: p for p in planned}
    assert set(by) == {"INTC_US_EQ", "ALNY_US_EQ", "NVDA_US_EQ", "PENNY_US_EQ", "PIE_US_EQ",
                       "AAPL_US_EQ"}                                             # no EUR stock
    assert by["PIE_US_EQ"].in_pie and not by["PIE_US_EQ"].ok
    assert by["AAPL_US_EQ"].ok and by["AAPL_US_EQ"].quantity == 3 and "0.5 sh in a pie" in by["AAPL_US_EQ"].note
    assert by["INTC_US_EQ"].ok and by["INTC_US_EQ"].quantity == D("19.5")       # whole position
    assert by["INTC_US_EQ"].body["quantity"] == -19.5 and by["INTC_US_EQ"].body["limitPrice"] == 50.5
    assert by["INTC_US_EQ"].body["timeValidity"] == "GOOD_TILL_CANCEL"
    alny = by["ALNY_US_EQ"]
    assert alny.ok and alny.quantity == D(10) and [o.order_id for o in alny.cancel] == ["o1"]
    assert alny.limit == D("153.22")
    assert by["NVDA_US_EQ"].keep and not by["NVDA_US_EQ"].ok                     # identical order kept
    assert "ALNY_US_EQ" not in t.validated                     # shares still blocked → not validated yet
    assert not t.cancelled and not t.placed                    # planning changes nothing

    failed = execute(t, planned)
    assert t.cancelled == ["o1"]                               # only ALNY's old order, NVDA kept
    assert [b["instrumentCode"] for b in t.placed] == ["AAPL_US_EQ", "ALNY_US_EQ", "PENNY_US_EQ"]
    assert failed == 1 and by["INTC_US_EQ"].result.startswith("FAILED: MarketClosed")
    assert by["ALNY_US_EQ"].result.startswith("sent, order")


def test_plan_eur_and_changed_order():
    t = FakeT212()
    planned = plan(t, "EUR", D(1))
    shown = [p for p in planned if not p.in_pie]
    assert [p.ticker for p in shown] == ["BAYNd_EQ"] and shown[0].limit == D("25.36")
    # same quantity but other validity → replaced, not kept
    t2 = FakeT212()
    t2.orders = [sell("o2", "NVDA_US_EQ", "10", "181.80", validity="DAY")]
    nv = {p.ticker: p for p in plan(t2, "USD", D(1))}["NVDA_US_EQ"]
    assert nv.ok and [o.order_id for o in nv.cancel] == ["o2"]
    # cancel fails → nothing new is placed
    t3 = FakeT212()
    t3.cancel_order = lambda oid, value_order=None, wait=False: SimpleNamespace(status="CANCELLING")
    p3 = [p for p in plan(t3, "USD", D(1)) if p.ticker == "ALNY_US_EQ"]
    assert execute(t3, p3) == 1 and "FAILED to cancel" in p3[0].result and not t3.placed


if __name__ == "__main__":
    test_limit_price()
    test_plan_us_and_execute()
    test_plan_eur_and_changed_order()
    print("OK")
