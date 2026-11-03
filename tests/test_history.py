"""History, details, taxes, gains – with responses from the capture (anonymised)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from test_offline import LOGIN_OK, _resp  # noqa: E402

from trading212 import Config, Trading212Client  # noqa: E402
from trading212.cli import main  # noqa: E402

FX = json.loads((Path(__file__).parent / "history_fixtures.json").read_text())


class Srv(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kw):
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        params = params or {}
        self.calls.append((method, path, dict(params), json))
        if path == "/rest/validate-session":
            return _resp(200, {}, url)
        if path == "/rest/history/v2/orders":
            if params.get("instrumentCode"):
                data = [x for x in FX["orders_page1"]["data"]
                        if x["heading"]["context"]["instrumentCode"] == params["instrumentCode"]]
                return _resp(200, {"data": data, "hasNext": False, "header": None}, url)
            if params.get("olderThan"):
                assert unquote(params["olderThan"]) == FX["orders_page1"]["data"][-1]["date"]
                return _resp(200, FX["orders_page2"] | {"hasNext": False}, url)
            return _resp(200, FX["orders_page1"], url)
        if path.startswith("/rest/history/v2/"):
            kind = path.rsplit("/", 1)[1]
            return _resp(200, FX.get(kind, FX["all_page1"]) | {"hasNext": False}, url)
        if path.startswith("/rest/history/v5/orders/"):
            return _resp(200, FX["order_details"], url)
        if path == "/rest/taxes/v2/overview":
            return _resp(200, FX["tax_overview"], url)
        if path.startswith("/rest/taxes/v1/pot-summary/pot/"):
            return _resp(200, FX["stock_loss_pot"], url)
        if path == "/rest/customer-taxes/v1/customers/exemption/nv-status":
            return _resp(200, {"nvCertificateHolder": "NONE"}, url)
        if path == "/rest/reports/v1/income-taxes/tax-transactions/search":
            if json:                                   # like the real server: filter body → 400
                return _resp(400, {"code": "MessageNotReadable"}, url)
            if "idCursor" in params:
                assert params["idCursor"] == FX["tax_search"][-1]["taxId"]
                return _resp(200, [], url)
            return _resp(200, FX["tax_search"], url)
        if path.startswith("/rest/reports/v1/income-taxes/tax-transactions/EQUITY/EQUITY_SOLD/"):
            return _resp(200, FX["tax_details"], url)
        if path == "/rest/v1/equity/multi-accounts/summary":
            return _resp(200, FX["summary_live"], url)
        return _resp(404, {"code": "NotFound", "path": path}, url)


def client(tmp):
    srv = Srv()
    t = Trading212Client(Config(session_file=Path(tmp) / "s.json"), session=srv)
    t._apply_login(LOGIN_OK)
    t._save_session_file()
    return t, srv


def test_history_pagination_and_details():
    with tempfile.TemporaryDirectory() as tmp:
        t, srv = client(tmp)
        entries = list(t.iter_history("orders"))
        n1 = len(FX["orders_page1"]["data"])
        assert len(entries) > n1                                     # second page loaded
        assert entries[0].label == "Sell" and entries[0].ticker == "MSFT_US_EQ"
        assert entries[0].amount == Decimal("348.79") and entries[0].currency == "USD"
        assert any(e.label == "Buy" and e.amount < 0 for e in entries)
        assert len(list(t.iter_history("orders", max_items=5))) == 5
        only = list(t.iter_history("orders", instrument="MSFT_US_EQ"))
        assert only and all(e.ticker == "MSFT_US_EQ" for e in only)
        assert srv.calls[-1][2]["instrumentCode"] == "MSFT_US_EQ"

        d = t.get_history_details(entries[0])
        assert srv.calls[-1][1] == "/rest/history/v5/orders/99319640949"
        assert d.status == "filled" and d.fill_price == Decimal("157.11") and d.price_currency == "USD"
        assert d.limit_price == Decimal("153.64") and d.filled_quantity == Decimal("2.22")
        assert d.return_amount == Decimal("5.35") and d.return_percent == Decimal("1.56")
        assert d.gain("EUR") == Decimal("4.80") and d.gain("USD") == Decimal("5.35")
        assert d.trade_confirmation == "/v1/statements/trade-confirmation/99319640949"
        assert t.get_history_summary("orders")[0].amount == Decimal("-42.61")


def test_taxes_and_gains():
    with tempfile.TemporaryDirectory() as tmp:
        t, srv = client(tmp)
        o = t.get_tax_overview()
        assert o.exemption_remaining == Decimal("931.23") and o.stock_loss_pot == Decimal("42.95")
        pot = t.get_loss_pot("stock")
        assert pot.remaining == Decimal("42.95")
        txs = list(t.iter_tax_transactions())
        assert len(txs) == len(FX["tax_search"])
        assert srv.calls[-1][2].get("idCursor")                     # second page requested
        sold = list(t.iter_tax_transactions(tax_types=["EQUITY_SOLD"]))
        assert sold and all(x.tax_type == "EQUITY_SOLD" for x in sold)
        assert all(c[3] == {} for c in srv.calls if c[1].endswith("/tax-transactions/search"))
        assert list(t.iter_tax_transactions(tax_types=["EQUITY_SOLD"], server_filter=True))   # falls back to {}
        nov = list(t.iter_tax_transactions(date_from="2026-11-02", date_to="2026-11-02"))
        assert nov and all(x.time_created.startswith("2026-11-02") for x in nov)
        gains = t.realized_gains(year=2026)
        assert gains and gains[0].gain["EUR"] == Decimal("4.80")
        pos = t.get_positions()
        assert len(pos) == len(FX["summary_live"]["accountsByType"]["EQUITY"]["open"])


def test_cli_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        t, srv = client(tmp)
        import trading212.cli as cli
        orig = cli.Trading212Client
        cli.Trading212Client = lambda cfg: Trading212Client(cfg, session=_copy(srv, t))
        try:
            Path(tmp, "c.toml").write_text("")
            base = ["--session-file", str(Path(tmp) / "s.json"), "-c", str(Path(tmp) / "c.toml")]
            for args in (["history"], ["history", "orders", "--details", "-n", "3"],
                         ["history", "dividends"], ["history", "transactions", "-n", "5"],
                         ["details", "99319640949"], ["taxes"], ["tax-transactions", "--year", "2026"],
                         ["gains", "--year", "2026"], ["positions"], ["history", "--json", "-n", "2"]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(base + args)
                out = buf.getvalue()
                assert rc == 0, (args, out)
                print(f"$ trading212 {' '.join(args)}\n{out[:1400]}")
            assert "00000000000" not in out          # tax id is only printed masked
        finally:
            cli.Trading212Client = orig


def _copy(srv, t):
    for c in t.http.cookies:
        srv.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    return srv


if __name__ == "__main__":
    test_history_pagination_and_details()
    test_taxes_and_gains()
    test_cli_outputs()
    print("OK")
