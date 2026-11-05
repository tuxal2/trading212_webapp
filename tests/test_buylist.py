"""Offline test of t212-buylist: parsing, sizing, planning and sending (no network).

    python tests/test_buylist.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t212_tools.buylist import parse_list, plan, quantity_for, send  # noqa: E402
from trading212 import Trading212Client  # noqa: E402
from trading212.exceptions import ApiError  # noqa: E402

LIST = "\n".join("\t".join(cols) for cols in [
    ["TEAM", "Atlassian Corp Plc", "US0494681010", "USD", "181.80", "181.80", "183.80",
     "MOUNT US Tech 100, SOLID US Tech 100, ARROW US Tech 100"],
    ["NVDA", "NVIDIA Corporation", "US67066G1040", "USD", "198.50", "198.50", "198.50", "ARROW US 100"],
    ["PANW", "Palo Alto Networks Inc", "US6974351057", "USD", "342.80", "342.80", "342.80", "ARROW US Tech 100"],
    ["INTC", "Intel Corporation", "US4581401001", "USD", "52.20", "52.20", "52.20", "ARROW US Tech 100, SOLID US 100"],
    ["CSCO", "Cisco Systems Inc", "US17275R1023", "USD", "1.000,00", "100.00", "100.00", "ARROW US 100"],
]) + "\nsome footer text 123\n"


def test_parse():
    rows, warnings = parse_list(LIST)
    assert [r.symbol for r in rows] == ["TEAM", "NVDA", "PANW", "INTC", "CSCO"]
    team = rows[0]
    assert team.isin == "US0494681010" and team.currency == "USD" and team.name == "Atlassian Corp Plc"
    assert team.prices == [Decimal("181.80"), Decimal("181.80"), Decimal("183.80")]
    assert team.limit == Decimal("181.80")                       # first column
    assert team.strategies.startswith("MOUNT US Tech 100")
    assert rows[4].limit == Decimal("1000.00")                   # German number format
    assert len(warnings) == 1 and "footer" in warnings[0]
    # copied with spaces instead of tabs
    rows2, _ = parse_list("NVDA   NVIDIA Corporation   US67066G1040   USD   198.50   198.50   198.50   ARROW US 100")
    assert rows2[0].isin == "US67066G1040" and rows2[0].limit == Decimal("198.50")
    # duplicates
    rows3, w3 = parse_list(LIST.splitlines()[1] + "\n" + LIST.splitlines()[1])
    assert len(rows3) == 1 and "twice" in w3[0]


def test_quantity():
    assert quantity_for(Decimal(1000), Decimal("181.80")) == 5     # 5.5 → 5, no fractions
    assert quantity_for(Decimal(1000), Decimal("1000.00")) == 1
    assert quantity_for(Decimal(300), Decimal("342.80")) == 0


class FakeT212:
    """Just the client methods the app uses."""

    is_demo = True

    def __init__(self):
        self.sent = []
        self.validated = []

    def get_orders(self):
        return [SimpleNamespace(ticker="NVDA_US_EQ", side="BUY"),
                SimpleNamespace(ticker="PANW_US_EQ", side="SELL")]

    def get_positions(self):
        return [SimpleNamespace(ticker="PANW_US_EQ", quantity=Decimal("10.0"))]

    def resolve_ticker(self, isin, currency=None):
        tickers = {"US0494681010": "TEAM", "US67066G1040": "NVDA", "US6974351057": "PANW",
                   "US4581401001": "INTC", "US17275R1023": "CSCO"}
        return SimpleNamespace(ticker=f"{tickers[isin]}_US_EQ", isin=isin, currency="USD")

    def settlement_currency(self, inst):
        return "USD"

    build_order = staticmethod(Trading212Client.build_order)

    def validate_order(self, body):
        self.validated.append(body)
        if body["instrumentCode"] == "INTC_US_EQ":
            raise ApiError(400, "BusinessException", "InsufficientFreeForStocksBuy", {}, "", "")

    def preview_order(self, body):
        return SimpleNamespace(total=Decimal(str(body["quantity"])) * Decimal(str(body["limitPrice"])))

    def place_order(self, body, validate=True):
        self.sent.append(body)
        return SimpleNamespace(order_id=str(1000 + len(self.sent)), status="NEW")


def test_plan_and_send():
    rows, _ = parse_list(LIST)
    t = FakeT212()
    planned = plan(t, rows, {"amount": {"usd": 1000}})
    by = {p.row.symbol: p for p in planned}
    assert by["TEAM"].ok and by["TEAM"].quantity == 5 and by["TEAM"].total == Decimal("909.00")
    assert by["TEAM"].body["limitPrice"] == 181.8 and by["TEAM"].body["timeValidity"] == "DAY"
    assert by["TEAM"].body["orderType"] == "LIMIT" and by["TEAM"].body["quantity"] > 0
    assert by["NVDA"].problem == "open buy order exists"         # skip_existing (default)
    assert by["PANW"].ok and by["PANW"].quantity == 2            # position + sell order: still bought
    assert by["PANW"].note == "holding 10 sh, open sell order"
    assert "InsufficientFreeForStocksBuy" in by["INTC"].problem
    assert by["CSCO"].ok and by["CSCO"].quantity == 1
    assert not t.sent                                            # planning never sends

    assert send(t, planned) == 0
    assert [b["instrumentCode"] for b in t.sent] == ["TEAM_US_EQ", "PANW_US_EQ", "CSCO_US_EQ"]
    assert by["TEAM"].result.startswith("sent, order 1001")

    # no amount for the currency → skipped, nothing validated
    t2 = FakeT212()
    planned2 = plan(t2, rows[:1], {"amount": {"EUR": 900}})
    assert "no amount for USD" in planned2[0].problem and not t2.validated
    # --amount override, skip_existing off
    planned3 = plan(FakeT212(), rows[1:2], {"skip_existing": False}, Decimal(400))
    assert planned3[0].ok and planned3[0].quantity == 2
    # skip_positions
    planned4 = plan(FakeT212(), rows[2:3], {"amount": 1000, "skip_positions": True})
    assert planned4[0].problem == "position held (skip_positions)"


if __name__ == "__main__":
    test_parse()
    test_quantity()
    test_plan_and_send()
    print("OK")
