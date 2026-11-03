"""Offline test: TOTP codes and the unattended re-login with 2FA (no network).

    python tests/test_totp.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading212 import Config, Trading212Client  # noqa: E402
from trading212.exceptions import AuthenticationError, ConfigError  # noqa: E402
from trading212.totp import totp  # noqa: E402

from test_offline import LOGIN_OK, TWO_FA, _resp  # noqa: E402

# RFC 6238, appendix B: secret "12345678901234567890" (SHA1)
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
NOW = 1111111109


def test_totp_rfc6238():
    assert totp(f"otpauth://totp/x?secret={RFC_SECRET}&digits=8", at=59) == "94287082"
    assert totp(f"otpauth://totp/x?secret={RFC_SECRET}&digits=8", at=NOW) == "07081804"
    assert totp(RFC_SECRET, at=59) == "287082"                      # default: 6 digits
    assert totp(RFC_SECRET.lower()[:8] + " " + RFC_SECRET.lower()[8:], at=59) == "287082"
    try:
        totp("not base32!")
        raise AssertionError("ConfigError expected")
    except ConfigError:
        pass


class Server(requests.Session):
    """Session expired; cookie re-auth and token login need 2FA."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kw):
        path = url.split(".com", 1)[1]
        self.calls.append((path, json))
        if path == "/rest/validate-session":
            ok = self.cookies.get("TRADING212_SESSION_LIVE") == "sess123"
            return _resp(200 if ok else 401, {}, url)
        if path == "/rest/v3/webclient/authenticate":
            return _resp(403, TWO_FA, url)
        if path == "/rest/v4/login/token":
            assert json["token"] == "tok-1"
            if "twoFactorAuth" not in json:
                return _resp(403, TWO_FA, url)
            assert json["twoFactorAuth"]["rememberDevice"] is False
            # current or previous 30 s window (the test may cross a boundary)
            assert json["twoFactorAuth"]["authenticationCode"] in (
                totp(RFC_SECRET), totp(RFC_SECRET, at=time.time() - 30))
            return _resp(200, LOGIN_OK, url)
        return _resp(404, {"code": "NotFound"}, url)


def _client(tmp, secret):
    srv = Server()
    t = Trading212Client(Config(session_file=Path(tmp) / "s.json", totp_secret=secret), session=srv)
    t._login_token = "tok-1"
    t.account_id, t.account_ids = 111, {"LIVE": 111}
    t.http.cookies.set("LOGIN_TOKEN", "cookie", domain=".trading212.com")
    return t, srv


def test_relogin_with_totp():
    with tempfile.TemporaryDirectory() as tmp:
        t, srv = _client(tmp, RFC_SECRET)
        t.ensure_login()
        assert t.validate_session()
        paths = [p for p, _ in srv.calls]
        assert paths.count("/rest/v4/login/token") == 2              # without, then with 2FA


def test_relogin_without_totp_fails():
    with tempfile.TemporaryDirectory() as tmp:
        t, _ = _client(tmp, None)
        try:
            t.ensure_login()
            raise AssertionError("AuthenticationError expected")
        except AuthenticationError as exc:
            assert exc.error_type == "TwoFactorAuthenticationRequired"


if __name__ == "__main__":
    test_totp_rfc6238()
    test_relogin_with_totp()
    test_relogin_without_totp_fails()
    print("OK")
