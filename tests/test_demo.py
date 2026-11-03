"""Switching LIVE ↔ DEMO (modelled on the capture: POST {target-host}/rest/v4/login/session)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import LOGIN_OK, SUMMARY, _resp  # noqa: E402

import requests  # noqa: E402

from trading212 import Config, Trading212Client  # noqa: E402

DEMO_LOGIN = json.loads(json.dumps(LOGIN_OK))
DEMO_LOGIN.update(accountId=222, accountSession="demo-sess", customerSession="demo-sess", loginToken=None,
                  rememberMeCookie=None, subSystem="DEMO", sessionCookieName="TRADING212_SESSION_DEMO")
DEMO_LOGIN["account"] = dict(LOGIN_OK["account"], id=222, type="DEMO")
DEMO_LOGIN["accounts"] = [DEMO_LOGIN["account"]]


class Srv(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kw):
        host = url.split("//", 1)[1].split("/", 1)[0]
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        self.calls.append((method, host, path, json, dict(headers or {})))
        demo = host.startswith("demo.")
        if path == "/rest/v4/login/token":
            return _resp(200, LOGIN_OK, url)
        if path == "/rest/validate-session":
            name, val = ("TRADING212_SESSION_DEMO", "demo-sess") if demo else ("TRADING212_SESSION_LIVE", "sess123")
            return _resp(200 if self.cookies.get(name) == val else 401, {}, url)
        if path == "/rest/v1/accounts":
            return _resp(200, {"liveAccounts": [LOGIN_OK["account"]], "demoAccounts": [DEMO_LOGIN["account"]]}, url)
        if path == "/rest/v4/login/session":
            assert headers["X-Trader-Login-Ver"] == "v2"
            if demo:
                assert json == {"accountId": 222} and "accountId=111" in headers["X-Trader-Client"]
                return _resp(200, DEMO_LOGIN, url)
            assert json == {"accountId": 111} and "accountId=222" in headers["X-Trader-Client"]
            return _resp(200, dict(LOGIN_OK, loginToken=None, rememberMeCookie=None), url)
        if path == "/rest/v1/equity/multi-accounts/summary":
            s = json_mod.loads(json_mod.dumps(SUMMARY))
            s["accountsByType"]["EQUITY"]["cash"]["total"] = 50000 if demo else 1000
            return _resp(200, s, url)
        return _resp(404, {"code": "NotFound"}, url)


json_mod = json


def test_switch_live_demo_live():
    with tempfile.TemporaryDirectory() as tmp:
        sf = Path(tmp) / "s.json"
        srv = Srv()
        t = Trading212Client(Config(session_file=sf), session=srv)
        t._login_token = "tok-1"
        t.ensure_login(account="live")
        assert t.sub_system == "LIVE" and t.base_url.startswith("https://live.")
        assert t.get_cash().total == 1000

        t.ensure_login(account="demo")
        assert t.is_demo and t.account_id == 222 and t.base_url.startswith("https://demo.")
        assert srv.calls[-1][1].startswith("demo.") and srv.calls[-1][2] == "/rest/v4/login/session"
        assert t.get_cash().total == 50000
        assert srv.calls[-1][1].startswith("demo.")              # follow-up calls go to the demo host
        assert t._login_token == "tok-1"                         # login token is kept

        # new instance: demo session from file, no new login
        srv2 = Srv()
        t2 = Trading212Client(Config(session_file=sf, account="demo"), session=srv2)
        t2.ensure_login()
        assert t2.is_demo and [c[2] for c in srv2.calls] == ["/rest/validate-session"]

        # back to LIVE
        t2.ensure_login(account="live")
        assert t2.sub_system == "LIVE" and t2.account_id == 111
        assert srv2.calls[-1][1].startswith("live.")
        assert t2.get_cash().total == 1000

        # demo session expired → check live session, then switch again
        srv3 = Srv()
        t3 = Trading212Client(Config(session_file=sf, account="demo"), session=srv3)
        for c in list(srv3.cookies):
            if c.name == "TRADING212_SESSION_DEMO":
                srv3.cookies.set(c.name, "expired", domain=c.domain, path=c.path)
        t3.sub_system, t3.account_id = "DEMO", 222
        t3.ensure_login()
        assert t3.is_demo and t3.get_cash().total == 50000


if __name__ == "__main__":
    test_switch_live_demo_live()
    print("OK")
