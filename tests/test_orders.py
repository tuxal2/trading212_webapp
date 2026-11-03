"""Offline test of the order functions (simulated responses, no network)."""

from __future__ import annotations

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import LOGIN_OK, SUMMARY, FakeSession, _resp  # noqa: E402

from trading212 import ApiError, Config, Trading212Client  # noqa: E402
from trading212.cli import main  # noqa: E402

NEW_ORDER = {"orderId": "900001", "type": "LIMIT", "code": "INTC_US_EQ", "currencyCode": "USD",
             "quantity": 2, "filledQuantity": 0, "status": "NEW", "limitPrice": 60,
             "created": "2026-11-02T20:49:34.257+02:00", "frontend": "WC4",
             "enabledExtendedMarketHours": False, "timeValidity": "DAY"}
OLD_SELL = {"orderId": "800001", "type": "LIMIT", "code": "XYZ_US_EQ", "currencyCode": "USD",
            "quantity": -30, "filledQuantity": 0, "status": "NEW", "limitPrice": 180,
            "created": "2026-11-02T20:00:22.315+02:00", "timeValidity": "GOOD_TILL_CANCEL"}
ALGOLIA_HIT = {"category": "EQUITY", "type": "STOCK", "shortName": "INTC", "name": "Intel",
               "fullName": "Intel Corp", "exchangeName": "NASDAQ", "currencyCode": "USD",
               "isin": "US4581401001", "objectID": "INTC_US_EQ",
               "state": {"tradable": True, "enabled": True, "suspended": False}}


class OrderSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.sent_orders = []
        self.deleted = []
        self.open = {NEW_ORDER["orderId"]: dict(NEW_ORDER), OLD_SELL["orderId"]: dict(OLD_SELL)}

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kw):
        path = url.split(".com", 1)[1] if ".com" in url else url
        if path == "/rest/instrument-search/v3/config":
            return _resp(200, {"credentials": {"applicationId": "APPID", "searchApiKey": "k"},
                               "profiles": {"search": {"indexName": "instruments.v2.EU",
                                                       "searchParams": {"filters": "x"}}}}, url)
        if "algolia.net" in url:
            cfd = dict(ALGOLIA_HIT, objectID="INTC", category="CFD", exchangeName="NASDAQ (CFD)")
            return _resp(200, {"results": [{"hits": [cfd, ALGOLIA_HIT]}]}, url)
        if path == "/rest/v2/public/added-costs":
            q = json["quantity"]; lp = json.get("limitPrice", 61)
            return _resp(200, {"orderQuantity": abs(q), "orderValue": abs(q) * lp, "sharesValue": abs(q) * lp,
                               "total": abs(q) * lp, "exchangeRate": None, "costs": {}}, url)
        if path == "/rest/public/v2/equity/order/validate":
            if json["quantity"] > 1000:
                return _resp(400, {"code": "BusinessException",
                                   "context": {"type": "InsufficientFreeForStocksBuy"}}, url)
            r = _resp(200, {}, url); r._content = b""; return r
        if path == "/rest/public/v2/equity/order":
            self.sent_orders.append(json)
            return _resp(200, {"account": {"equityOrders": [NEW_ORDER, OLD_SELL], "equityValueOrders": []}}, url)
        if method == "DELETE" and path.startswith("/rest/public/v2/equity/order/"):
            oid = path.rsplit("/", 1)[1]
            if oid not in self.open:
                return _resp(400, {"code": "BusinessException", "context": {"type": "OrderNotFound"}}, url)
            self.deleted.append(oid)
            self.open[oid] = dict(self.open[oid], status="CANCELLING")   # as in the capture
            return _resp(200, {"account": {"equityOrders": list(self.open.values()),
                                           "equityValueOrders": []}}, url)
        if path == "/rest/v1/equity/order-details":
            return _resp(200, {"orderQuantity": 2, "orderValue": 120.00, "sharesValue": 120.00,
                               "total": 120.00, "costs": {}}, url)
        if path == "/rest/v1/equity/multi-accounts/summary":
            s = json_copy(SUMMARY)
            s["accountsByType"]["EQUITY"]["orders"] = list(self.open.values())
            # cancellation is complete on the next request
            self.open = {k: v for k, v in self.open.items() if v.get("status") != "CANCELLING"}
            return _resp(200, s, url)
        return super().request(method, url, headers=headers, params=params, json=json, timeout=timeout)


def json_copy(o):
    return json.loads(json.dumps(o))


def logged_in(tmp):
    sf = Path(tmp) / "s.json"
    fake = OrderSession()
    t = Trading212Client(Config(username="user@example.com", password="pw", session_file=sf), session=fake)
    t.login(otp="123456")
    return t, fake, sf


def test_build_order_matches_web_client():
    body = Trading212Client.build_order("INTC_US_EQ", 2, side="BUY", order_type="LIMIT", currency="USD",
                                        limit_price=60, time_validity="DAY", extended_hours=True)
    # exactly the body from the browser capture
    assert body == {"quantity": 2, "instrumentCode": "INTC_US_EQ", "currencyCode": "USD",
                    "orderType": "LIMIT", "limitPrice": 60, "timeValidity": "DAY",
                    "enabledExtendedMarketHours": True}
    sell = Trading212Client.build_order("X", Decimal("1.5"), side="SELL", order_type="STOP_LIMIT",
                                        currency="EUR", limit_price="9.95", stop_price=10)
    assert sell["quantity"] == -1.5 and sell["limitPrice"] == 9.95 and sell["stopPrice"] == 10
    for bad in (dict(order_type="LIMIT"), dict(order_type="STOP"), dict(time_validity="WEEK")):
        try:
            Trading212Client.build_order("X", 1, currency="EUR", **bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_place_cancel_and_list():
    with tempfile.TemporaryDirectory() as tmp:
        t, fake, _ = logged_in(tmp)
        o = t.buy_limit("INTC_US_EQ", 2, 60, currency="USD")
        assert o.order_id == "900001" and o.side == "BUY" and o.limit_price == 60
        assert fake.sent_orders[-1]["timeValidity"] == "DAY"
        orders = t.get_orders()
        assert {x.order_id: x.side for x in orders} == {"900001": "BUY", "800001": "SELL"}
        assert t.get_order_details("900001").total == Decimal("120.00")
        res = t.cancel_order("900001", value_order=False)          # without waiting
        assert fake.deleted == ["900001"] and res.status == "CANCELLING"
        assert t.get_order("900001").status == "CANCELLING"        # first request: still in progress
        assert t.get_order("900001") is None                       # gone afterwards
        try:
            t.cancel_order("900001")
            raise AssertionError("already cancelled")
        except ApiError as e:
            assert e.code == "OrderNotFound"
        fake.open["900001"] = dict(NEW_ORDER)
        assert t.cancel_order("900001", wait=True, poll=0.01) is None
        fake.open["900001"] = dict(NEW_ORDER)
        done = t.cancel_orders(side="SELL", wait=True)
        assert [o.order_id for o, _ in done] == ["800001"] and "900001" in fake.open
        fake.open["800001"] = dict(OLD_SELL)
        try:
            t.buy_limit("INTC_US_EQ", 5000, 60, currency="USD")
            raise AssertionError("validation should have failed")
        except ApiError as e:
            assert e.error_type == "InsufficientFreeForStocksBuy"
        assert len(fake.sent_orders) == 1     # invalid order was not sent
        assert t.resolve_ticker("INTC").ticker == "INTC_US_EQ"


def test_cli_order(monkeypatch=None):
    import trading212.cli as cli
    with tempfile.TemporaryDirectory() as tmp:
        t, fake, sf = logged_in(tmp)
        orig = cli.Trading212Client
        cli.Trading212Client = lambda cfg: Trading212Client(cfg, session=_with_cookies(fake, t))
        try:
            base = ["--session-file", str(sf), "-c", str(Path(tmp) / "none.toml")]
            Path(tmp, "none.toml").write_text("")
            assert main(base + ["order", "buy", "INTC", "2", "--limit", "60", "--dry-run"]) == 0
            assert len(fake.sent_orders) == 0           # dry run sends nothing
            assert main(base + ["order", "buy", "INTC", "2", "--limit", "60", "-y"]) == 0
            assert fake.sent_orders[-1] == {"quantity": 2, "instrumentCode": "INTC_US_EQ",
                                            "currencyCode": "USD", "orderType": "LIMIT", "limitPrice": 60,
                                            "timeValidity": "DAY", "enabledExtendedMarketHours": False}
            assert main(base + ["order", "buy", "INTC", "2", "--limit", "60"]) == 0   # without -y, without confirmation
            assert len(fake.sent_orders) == 2
            assert main(base + ["order", "buy", "INTC", "2", "--limit", "60", "--confirm"]) == 1  # no TTY → abort
            assert len(fake.sent_orders) == 2
            assert main(base + ["orders"]) == 0
            fake.open["900001"] = dict(NEW_ORDER)
            assert main(base + ["cancel", "900001"]) == 0
            assert main(base + ["cancel", "900001"]) == 1                 # no longer open
            fake.open["900001"] = dict(NEW_ORDER)
            assert main(base + ["cancel", "--symbol", "INTC", "--side", "buy"]) == 0
            assert "900001" not in fake.open and "800001" in fake.open
            assert main(base + ["cancel", "--all"]) == 0
            assert fake.open == {}
        finally:
            cli.Trading212Client = orig


def _with_cookies(fake, t):
    for c in t.http.cookies:
        fake.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    return fake


if __name__ == "__main__":
    test_build_order_matches_web_client()
    test_place_cancel_and_list()
    test_cli_order()
    print("OK")


def test_cli_order_without_preview():
    """'No price available' in the cost preview must not block the order."""
    import trading212.cli as cli
    with tempfile.TemporaryDirectory() as tmp:
        t, fake, sf = logged_in(tmp)
        orig_req = fake.request

        def req(method, url, **kw):
            if url.endswith("/rest/v2/public/added-costs"):
                return _resp(400, {"code": "No price available for instrument"}, url)
            return orig_req(method, url, **kw)
        fake.request = req
        orig = cli.Trading212Client
        cli.Trading212Client = lambda cfg: Trading212Client(cfg, session=_with_cookies(fake, t))
        try:
            Path(tmp, "none.toml").write_text("")
            base = ["--session-file", str(sf), "-c", str(Path(tmp) / "none.toml")]
            assert main(base + ["order", "buy", "INTC", "2", "--limit", "60", "-y"]) == 0
            assert len(fake.sent_orders) == 1
        finally:
            cli.Trading212Client = orig


if __name__ == "__main__":
    test_cli_order_without_preview()
    print("OK2")


def test_sell_us_settles_in_usd():
    """Capture place_sell_order: sell 60 INTC_US_EQ limit 63, day, settled in USD."""
    import trading212.cli as cli
    from trading212.models import OrderCosts
    # cost preview with FX rate (settlement EUR) from the capture must be parseable
    c = OrderCosts.from_json({"orderQuantity": -60, "orderValue": 3374.13, "sharesValue": 3374.13,
                              "total": 3368.99, "exchangeRate": {"fromCurrency": "USD", "toCurrency": "EUR",
                                                                  "rate": 0.89263},
                              "costs": {"TRANSACTION_FEE": 0.07, "CURRENCY_CONVERSION_FEE": 5.06,
                                        "FINRA_FEE": 0.01}})
    assert c.exchange_rate == Decimal("0.89263") and c.rate_from == "USD"

    with tempfile.TemporaryDirectory() as tmp:
        t, fake, sf = logged_in(tmp)
        orig = cli.Trading212Client
        cli.Trading212Client = lambda cfg: Trading212Client(cfg, session=_with_cookies(fake, t))
        try:
            Path(tmp, "none.toml").write_text("")
            base = ["--session-file", str(sf), "-c", str(Path(tmp) / "none.toml")]
            assert main(base + ["order", "sell", "INTC", "60", "--limit", "63", "--extended"]) == 0
            assert fake.sent_orders[-1] == {"quantity": -60, "instrumentCode": "INTC_US_EQ",
                                            "currencyCode": "USD", "orderType": "LIMIT", "limitPrice": 63,
                                            "timeValidity": "DAY", "enabledExtendedMarketHours": True}
            # explicitly EUR → currencyCode EUR, limit stays in USD
            assert main(base + ["order", "sell", "INTC", "60", "--limit", "63", "--settle", "EUR"]) == 0
            assert fake.sent_orders[-1]["currencyCode"] == "EUR" and fake.sent_orders[-1]["limitPrice"] == 63
        finally:
            cli.Trading212Client = orig
        # Python-API
        inst = t.resolve_ticker("INTC")
        assert t.settlement_currency(inst) == "USD"
        t.config.settlement_currency = "EUR"
        assert t.settlement_currency(inst) == "EUR"
        t.config.settlement_currency = "instrument"


if __name__ == "__main__":
    test_sell_us_settles_in_usd()
    print("OK3")
