"""Offline test with simulated server responses (no real data, no network).

    python -m pytest tests/   or   python tests/test_offline.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading212 import Config, Trading212Client, TwoFactorRequired  # noqa: E402

LOGIN_OK = {
    "tradingType": "EQUITY", "accountId": 111, "accountSession": "sess123",
    "customerSession": "sess123", "email": "user@example.com",
    "rememberMeCookie": {"path": "/", "domain": "trading212.com", "value": "cmVtZW1iZXI="},
    "subSystem": "LIVE", "customerId": "uuid-1", "loginToken": "tok-1",
    "sessionCookieName": "TRADING212_SESSION_LIVE", "customerCookieName": "CUSTOMER_SESSION",
    "customer": {"id": 9, "uuid": "uuid-1", "email": "user@example.com", "dealer": "T212DE"},
    "account": {"id": 111, "type": "LIVE", "tradingType": "EQUITY", "status": "ACTIVE",
                "currencyCode": "EUR", "readyToTrade": True, "accountDealer": "T212DE"},
    "accounts": [{"id": 111, "type": "LIVE", "tradingType": "EQUITY", "status": "ACTIVE",
                  "currencyCode": "EUR", "readyToTrade": True, "accountDealer": "T212DE"}],
}
TWO_FA = {"code": "BusinessException", "context": {"type": "TwoFactorAuthenticationRequired",
                                                   "authenticationType": "otp", "attemptId": "a-1"}}
SUMMARY = {"activeAccountCurrency": "EUR", "accountsByType": {"EQUITY": {
    "accountCurrency": "EUR",
    "cash": {"ppl": -10.5, "result": 2.0, "total": 1000.00, "blockedForStocks": 0.00,
             "pieCash": 0.07, "stockInvestment": 600.0, "freeForStocks": 390.43,
             "netDeposit": 1005.0, "spendingPot": 9.5, "investPot": 390.43},
    "orders": [], "open": []}}}
WALLETS = {"EUR": {"freeForStocks": 320.0, "spendingPot": 9.5, "investPot": 320.0,
                   "primary": True, "blockedForStocks": 0.0, "blockedByTicker": {}},
           "USD": {"freeForStocks": 80.0, "spendingPot": 0.0, "investPot": 80.0, "primary": False,
                   "converted": {"currencyCode": "EUR", "freeForStocks": 70.43},
                   "blockedForStocks": 0.0, "blockedByTicker": {}}}


class FakeSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.login_attempts = 0

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kw):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, dict(headers or {}), json))
        if path == "/rest/v5/login":
            self.login_attempts += 1
            if "twoFactorAuth" not in json:
                return _resp(403, TWO_FA, url)
            assert json["twoFactorAuth"]["authenticationCode"] == "123456"
            return _resp(200, LOGIN_OK, url)
        if path == "/rest/validate-session":
            ok = self.cookies.get("TRADING212_SESSION_LIVE") == "sess123"
            return _resp(200 if ok else 401, {} if ok else {"code": "BusinessException",
                                                            "context": {"type": "InvalidSession"}}, url)
        assert headers.get("X-Trader-Target-Type") == "EQUITY"
        assert "accountId=111" in headers["X-Trader-Client"]
        if path == "/rest/v1/equity/multi-accounts/summary":
            assert params == {"targetCurrency": "EUR"} and json == []
            return _resp(200, SUMMARY, url)
        if path == "/rest/trading/v1/account/wallets":
            return _resp(200, WALLETS, url)
        return _resp(404, {"code": "NotFound"}, url)


def _resp(status, payload, url):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload).encode()
    r.url = url
    r.headers["Content-Type"] = "application/json"
    return r


def test_login_2fa_and_budget():
    with tempfile.TemporaryDirectory() as tmp:
        sf = Path(tmp) / "session.json"
        cfg = Config(username="user@example.com", password="pw", session_file=sf)
        fake = FakeSession()
        t = Trading212Client(cfg, session=fake)

        try:
            t.login()
            raise AssertionError("2FA expected")
        except TwoFactorRequired as e:
            assert e.context["attemptId"] == "a-1"

        r = t.login(otp_callback=lambda ctx: "123456")
        assert r.account_id == 111 and r.login_token == "tok-1"
        login_headers = fake.calls[-1][2]
        assert login_headers["X-Trader-Login-Ver"] == "v2"
        assert "accountId" not in login_headers["X-Trader-Client"]

        b = t.get_budget()
        assert b.available == Decimal("390.43")
        assert b.cash.total == Decimal("1000.00")
        assert [w.currency for w in b.wallets] == ["EUR", "USD"]
        assert b.wallets[1].converted_free_for_stocks == Decimal("70.43")

        # session file: mode 600, no password inside
        assert oct(sf.stat().st_mode & 0o777) == "0o600"
        saved = sf.read_text()
        assert "pw" not in json.loads(saved).values()
        assert "password" not in saved

        # new client uses the stored session without logging in again
        fake2 = FakeSession()
        t2 = Trading212Client(Config(username="user@example.com", session_file=sf), session=fake2)
        t2.ensure_login()
        assert fake2.login_attempts == 0
        assert t2.device_id == t.device_id
        assert t2.get_budget().available == Decimal("390.43")


if __name__ == "__main__":
    test_login_2fa_and_budget()
    print("OK")
