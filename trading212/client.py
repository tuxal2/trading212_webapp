"""Client for the (unofficial) web API of app.trading212.com.

Reconstructed from network captures of the web client (WC4, v8.54.0):

  Login
    POST /rest/v5/login                        username/password (+ 2FA)
    POST /rest/v4/login/token                  login with a stored loginToken
    POST /rest/v3/webclient/authenticate       new session from the LOGIN_TOKEN cookie (no 2FA
                                               on a trusted device)
    GET  /rest/validate-session                is the session still valid?
    POST {live|demo}.services…/rest/v4/login/session   switch LIVE ↔ DEMO
  Orders
    POST   /rest/v1/equity/order/instruments/settings   limits (max buy/sell, minimum quantity)
    POST   /rest/v2/public/added-costs                  cost preview
    POST   /rest/public/v2/equity/order/validate        validate an order (without placing it)
    POST   /rest/public/v2/equity/order                 quantity order
    DELETE /rest/public/v2/equity/order/{id}            cancel an order (asynchronous → CANCELLING)
    GET    /rest/v1/equity/order-details?orderId=       quantity/value/costs of an order
    POST   /rest/v1/equity/value-order[/review|/validate]  value (amount) order
    DELETE /rest/v1/equity/value-order/{id}
    POST   /instrumentarium/v1/prices/last-trade        last prices
    GET    /rest/instrument-search/v3/config            search configuration (Algolia)
  Account / budget
    GET  /rest/v1/accounts                     LIVE and DEMO accounts
    GET  /rest/v1/customers/accounts/summary   net worth across all accounts
    POST /rest/v1/equity/multi-accounts/summary?targetCurrency=EUR
                                               cash, open orders, positions
    GET  /rest/trading/v1/account/wallets      balance per currency

The session is carried by cookies (``TRADING212_SESSION_LIVE``,
``CUSTOMER_SESSION``, ``LOGIN_TOKEN``) and the ``X-Trader-*`` headers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import requests

from .config import Config
from .exceptions import (
    ApiError,
    AuthenticationError,
    ConfigError,
    InvalidSession,
    NetworkError,
    TwoFactorRequired,
)
from .history import HistoryMixin
from .totp import totp
from .models import (
    ORDER_TYPES,
    TIME_VALIDITIES,
    Account,
    Budget,
    Cash,
    Instrument,
    InstrumentSettings,
    LoginResult,
    Order,
    OrderCosts,
    Wallet,
)

log = logging.getLogger(__name__)

COOKIE_DOMAIN = ".trading212.com"
LOGIN_TOKEN_COOKIE = "LOGIN_TOKEN"

OtpCallback = Callable[[dict[str, Any]], str]


class Trading212Client(HistoryMixin):
    """Trading212 web client: login, budget, orders, history, taxes.

    Example::

        cfg = Config.load("config.toml")
        with Trading212Client(cfg) as t212:
            t212.ensure_login()          # session from 'python -m trading212 login'
            print(t212.get_budget().available)
    """

    def __init__(self, config: Config | None = None, session: requests.Session | None = None) -> None:
        self.config = config or Config.load()
        self.http = session or requests.Session()
        self.device_id: str = str(uuid.uuid4())
        self.login_result: LoginResult | None = None
        self.account_id: int | None = None
        self.trading_type: str = "EQUITY"
        self.currency: str = self.config.target_currency
        self._login_token: str | None = None
        self.sub_system: str = "LIVE"                    # LIVE | DEMO
        self.account_ids: dict[str, int] = {}            # {"LIVE": 500..., "DEMO": 500...}
        self._load_session_file()

    # ------------------------------------------------------------------ context
    def __enter__(self) -> "Trading212Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ headers
    def _client_header(self, with_account: bool) -> str:
        parts = [
            f"application={self.config.application}",
            f"version={self.config.app_version}",
            f"dUUID={self.device_id}",
        ]
        if with_account and self.account_id is not None:
            parts.append(f"accountId={self.account_id}")
        return ",".join(parts)

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": self.config.accept_language,
            "Origin": self.config.app_origin,
            "Referer": self.config.app_origin + "/",
            "User-Agent": self.config.user_agent,
            "X-Trader-Device-Model": self.config.device_model,
        }

    def _login_headers(self) -> dict[str, str]:
        h = self._base_headers()
        h["X-Trader-Client"] = self._client_header(with_account=False)
        h["X-Trader-Login-Ver"] = "v2"
        if self.config.turnstile_token:
            h["X-Trader-TST"] = self.config.turnstile_token
        h.update(self.config.extra_login_headers)
        return h

    def _auth_headers(self) -> dict[str, str]:
        h = self._base_headers()
        h["X-Trader-Client"] = self._client_header(with_account=True)
        h["X-Trader-Target-Type"] = self.trading_type
        return h

    # ------------------------------------------------------------------ hosts
    def host_for(self, sub_system: str) -> str:
        """Basis-URL je Subsystem: live.services… bzw. demo.services…"""
        base = self.config.base_url.rstrip("/")
        if sub_system.upper() == "DEMO":
            return base.replace("://live.", "://demo.")
        return base.replace("://demo.", "://live.")

    @property
    def base_url(self) -> str:
        return self.host_for(self.sub_system)

    @property
    def is_demo(self) -> bool:
        return self.sub_system == "DEMO"

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, headers: dict[str, str],
                 params: dict | None = None, json_body: Any = None,
                 expected_errors: bool = False, base: str | None = None) -> requests.Response:
        url = (base or self.base_url).rstrip("/") + path
        try:
            resp = self.http.request(method, url, headers=headers, params=params,
                                     json=json_body, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise NetworkError(f"Verbindung zu {url} fehlgeschlagen: {exc}") from exc
        log.debug("%s %s -> %s", method, path, resp.status_code)
        if resp.ok or expected_errors:
            return resp
        raise self._to_error(resp)

    @staticmethod
    def _to_error(resp: requests.Response) -> ApiError:
        code = err_type = None
        context: dict[str, Any] = {}
        try:
            data = resp.json()
            code = data.get("code")
            context = data.get("context") or {}
            err_type = context.get("type")
        except (ValueError, AttributeError):
            pass
        args = (resp.status_code, code, err_type, context, resp.url, resp.text[:2000])
        if err_type == "TwoFactorAuthenticationRequired":
            return TwoFactorRequired(*args)
        if err_type == "InvalidSession" or resp.status_code == 401:
            return InvalidSession(*args)
        if code == "AuthenticationFailed" or err_type in {"AuthenticationFailed", "InvalidToken",
                                                          "InvalidCredentials", "AccountDeactivated"}:
            return AuthenticationError(*args)
        return ApiError(*args)

    @staticmethod
    def _json(resp: requests.Response) -> Any:
        return resp.json(parse_float=Decimal)

    def _get(self, path: str, params: dict | None = None) -> Any:
        self._require_login()
        return self._json(self._request("GET", path, headers=self._auth_headers(), params=params))

    def _post(self, path: str, body: Any = None, params: dict | None = None) -> Any:
        self._require_login()
        return self._json(self._request("POST", path, headers=self._auth_headers(),
                                        params=params, json_body=body))

    def _delete(self, path: str) -> Any:
        self._require_login()
        resp = self._request("DELETE", path, headers=self._auth_headers())
        return self._json(resp) if resp.content else None

    def _require_login(self) -> None:
        if self.account_id is None:
            raise InvalidSession(401, "NotLoggedIn", None, None, "", "Nicht eingeloggt")

    # ------------------------------------------------------------------ login
    def login(self, username: str | None = None, password: str | None = None, *,
              otp: str | None = None, otp_callback: OtpCallback | None = None,
              remember_me: bool | None = None, remember_device: bool | None = None) -> LoginResult:
        """Login with username/password (``POST /rest/v5/login``).

        Needs a Cloudflare Turnstile token (``config.turnstile_token``), otherwise the
        server answers ``InvalidCredentials`` – normally use the browser login instead
        (``python -m trading212 login``). If the server requires 2FA, ``otp`` is used or ``otp_callback(context)`` is called.
        ``context`` contains e.g. ``authenticationType`` and ``attemptId``.
        """
        username = username or self.config.username
        password = password or self.config.password
        if not username or not password:
            raise ConfigError("Username and password are missing (parameters, T212_* environment or config).")

        log.debug("Password login for %s – password length %d, X-Trader-TST %s",
                  username, len(password), "set" if self.config.turnstile_token else "NOT set")
        body: dict[str, Any] = {
            "username": username,
            "password": password,
            "rememberMe": self.config.remember_me if remember_me is None else remember_me,
        }
        live = self.host_for("LIVE")
        resp = self._request("POST", "/rest/v5/login", headers=self._login_headers(),
                             json_body=body, expected_errors=True, base=live)

        if not resp.ok:
            err = self._to_error(resp)
            if err.error_type == "InvalidCredentials":
                err.args = (str(err) + "\nHint: in the shell, always pass passwords containing $ ` \\ \" ! "
                            "in 'single' quotes, or put them in the config / prompt. "
                            "With -v the password length used is logged."
                            + ("" if self.config.turnstile_token else
                               "\nWithout a captcha token (X-Trader-TST) the server usually rejects "
                               "the password login → run 'python -m trading212 browser-login' once."),)
                raise err
            if not isinstance(err, TwoFactorRequired):
                raise err
            code = otp or (otp_callback(err.context) if otp_callback else None)
            if not code:
                raise err
            body["twoFactorAuth"] = {
                "rememberDevice": self.config.remember_device if remember_device is None else remember_device,
                "authenticationCode": str(code).strip(),
            }
            resp = self._request("POST", "/rest/v5/login", headers=self._login_headers(),
                                 json_body=body, base=live)

        result = self._apply_login(self._json(resp))
        self._save_session_file()
        return result

    def login_with_token(self, token: str | None = None, account_id: int | None = None, *,
                         otp: str | None = None) -> LoginResult:
        """Login with the stored ``loginToken`` (``POST /rest/v4/login/token``).

        If the server asks for 2FA, ``otp`` is sent, or a code generated from
        ``config.totp_secret`` (as the web app does, with ``rememberDevice: false``).
        """
        token = token or self._login_token
        if not token:
            raise AuthenticationError(401, "NoToken", None, None, "", "No loginToken available")
        body: dict[str, Any] = {"token": token, "frontend": self.config.application}
        acc = account_id or self.account_ids.get("LIVE")
        if acc:
            body["accountId"] = acc
        live = self.host_for("LIVE")
        resp = self._request("POST", "/rest/v4/login/token", headers=self._login_headers(),
                             json_body=body, expected_errors=True, base=live)
        if not resp.ok:
            err = self._to_error(resp)
            if not isinstance(err, TwoFactorRequired) or not (otp or self.config.totp_secret):
                raise err
            log.info("Token login needs 2FA – sending %s code.", "the given" if otp else "a TOTP")
            body["twoFactorAuth"] = {"rememberDevice": False,
                                     "authenticationCode": otp or totp(self.config.totp_secret)}
            resp = self._request("POST", "/rest/v4/login/token", headers=self._login_headers(),
                                 json_body=body, base=live)
        result = self._apply_login(self._json(resp))
        self._save_session_file()
        return result

    def has_remember_cookie(self) -> bool:
        return any(c.name == LOGIN_TOKEN_COOKIE for c in self.http.cookies)

    def reauthenticate(self) -> LoginResult:
        """New session from the ``LOGIN_TOKEN`` cookie (``POST /rest/v3/webclient/authenticate``).

        This is what the web app does on start. Works without password and captcha; on a
        device that is not trusted the server answers with ``TwoFactorRequired``.
        """
        if not self.has_remember_cookie():
            raise AuthenticationError(401, "NoLoginTokenCookie", None, None, "", "No LOGIN_TOKEN cookie")
        resp = self._request("POST", "/rest/v3/webclient/authenticate", headers=self._login_headers(),
                             base=self.host_for("LIVE"))
        result = self._apply_login(self._json(resp))
        self._save_session_file()
        return result

    def validate_session(self) -> bool:
        """``GET /rest/validate-session`` – True if the cookie session is still valid."""
        if self.account_id is None:
            return False
        resp = self._request("GET", "/rest/validate-session", headers=self._auth_headers(),
                             expected_errors=True)
        return resp.ok

    def ensure_login(self, *, account: str | None = None) -> None:
        """Stored session → LOGIN_TOKEN cookie → token login; then switch LIVE/DEMO if needed.

        Never asks for a password or 2FA code, so it is safe for unattended runs. Raises
        ``AuthenticationError`` if no stored login works – then log in through the browser.

        ``account``: ``"live"`` or ``"demo"`` (default: ``config.account``).
        """
        target = (account or self.config.account or "live").upper()
        if target not in ("LIVE", "DEMO"):
            raise ConfigError(f"account must be 'live' or 'demo', not {account!r}")

        if self.validate_session():
            log.info("Stored %s session is valid.", self.sub_system)
        else:
            # demo session expired? then try the live session first
            if self.sub_system != "LIVE" and self.account_ids.get("LIVE"):
                self.sub_system, self.account_id = "LIVE", self.account_ids["LIVE"]
            if not self.validate_session():
                self._relogin()
        if self.sub_system != target:
            self.switch_account(target)

    def _relogin(self) -> None:
        last: ApiError | None = None
        if self.has_remember_cookie():
            try:
                self.reauthenticate()
                log.info("Re-authenticated with the LOGIN_TOKEN cookie.")
                return
            except ApiError as exc:
                log.info("Re-authentication with the LOGIN_TOKEN cookie failed (%s).", exc)
                last = exc
        if self._login_token:
            try:
                self.login_with_token()
                log.info("Token login successful.")
                return
            except ApiError as exc:
                log.info("Token login failed (%s).", exc)
                last = exc
        raise last or AuthenticationError(401, "NoStoredLogin", None, None, "",
                                          "No stored login – run 'python -m trading212 login'")

    def switch_account(self, target: str) -> LoginResult:
        """Switch between LIVE and DEMO (``POST {target-host}/rest/v4/login/session``).

        As in the web client, the request goes to the target system's server
        (``demo.services…`` or ``live.services…``) with the target account id and returns
        a separate session (cookie ``TRADING212_SESSION_DEMO`` / ``…_LIVE``).
        """
        target = target.upper()
        if target not in ("LIVE", "DEMO"):
            raise ConfigError("target must be LIVE or DEMO")
        self._require_login()
        if target == self.sub_system:
            return self.login_result  # type: ignore[return-value]
        acc_id = self.account_ids.get(target)
        if not acc_id:
            accs = self.get_accounts()[target.lower()]
            same = [a for a in accs if a.trading_type == self.trading_type] or accs
            if not same:
                raise ApiError(404, "NoAccount", None, None, "", f"No {target} account available")
            acc_id = same[0].id
        headers = self._auth_headers()
        headers["X-Trader-Login-Ver"] = "v2"
        resp = self._request("POST", "/rest/v4/login/session", headers=headers,
                             json_body={"accountId": acc_id}, base=self.host_for(target))
        result = self._apply_login(self._json(resp))
        log.info("Switched to %s (account %s)", result.sub_system, result.account_id)
        self._save_session_file()
        return result

    def logout_local(self) -> None:
        """Forget the local session (cookies + session file)."""
        self.http.cookies.clear()
        self.account_id = None
        self.login_result = None
        self._login_token = None
        self.sub_system = "LIVE"
        self.account_ids = {}
        sf = self.config.session_file
        if sf and Path(sf).exists():
            Path(sf).unlink()

    def _apply_login(self, data: dict) -> LoginResult:
        result = LoginResult.from_json(data)
        self.login_result = result
        self.account_id = result.account_id
        self.trading_type = result.trading_type or "EQUITY"
        self.currency = result.currency or self.config.target_currency
        self.sub_system = (result.sub_system or "LIVE").upper()
        self.account_ids[self.sub_system] = result.account_id
        if result.login_token:
            self._login_token = result.login_token

        # the web client sets these cookies itself via JavaScript
        jar = self.http.cookies
        jar.set(result.session_cookie_name, result.account_session, domain=COOKIE_DOMAIN, path="/")
        jar.set(result.customer_cookie_name, result.customer_session, domain=COOKIE_DOMAIN, path="/")
        rmc = result.remember_me_cookie
        if rmc and rmc.get("value"):
            jar.set(LOGIN_TOKEN_COOKIE, rmc["value"],
                    domain="." + rmc.get("domain", "trading212.com").lstrip("."),
                    path=rmc.get("path", "/"))
        return result

    # ------------------------------------------------------------------ session file
    def _load_session_file(self) -> None:
        sf = self.config.session_file
        if not sf or not Path(sf).exists():
            return
        try:
            data = json.loads(Path(sf).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Session file %s not readable: %s", sf, exc)
            return
        if self.config.username and data.get("username") and data["username"] != self.config.username:
            log.info("Session file belongs to another user – ignored.")
            return
        self.device_id = data.get("device_id") or self.device_id
        self.account_id = data.get("account_id")
        self.sub_system = (data.get("sub_system") or "LIVE").upper()
        self.account_ids = {k: int(v) for k, v in (data.get("account_ids") or {}).items()}
        if self.account_id and self.sub_system not in self.account_ids:
            self.account_ids[self.sub_system] = int(self.account_id)
        self.trading_type = data.get("trading_type") or self.trading_type
        self.currency = data.get("currency") or self.currency
        self._login_token = data.get("login_token")
        for c in data.get("cookies", []):
            self.http.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

    def _save_session_file(self) -> None:
        sf = self.config.session_file
        if not sf:
            return
        path = Path(sf)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "username": self.config.username,
            "device_id": self.device_id,
            "account_id": self.account_id,
            "sub_system": self.sub_system,
            "account_ids": self.account_ids,
            "trading_type": self.trading_type,
            "currency": self.currency,
            "login_token": self._login_token,
            "cookies": [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in self.http.cookies
                if c.domain and c.domain.lstrip(".").endswith("trading212.com")
            ],
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    # ------------------------------------------------------------------ account data
    def get_accounts(self) -> dict[str, list[Account]]:
        """``GET /rest/v1/accounts`` → ``{"live": [...], "demo": [...]}``."""
        data = self._get("/rest/v1/accounts")
        return {
            "live": [Account.from_json(a) for a in data.get("liveAccounts", [])],
            "demo": [Account.from_json(a) for a in data.get("demoAccounts", [])],
        }

    def get_accounts_overview(self) -> dict:
        """``GET /rest/v1/customers/accounts/summary`` – net worth per sub-system (raw)."""
        return self._get("/rest/v1/customers/accounts/summary")

    def get_net_worth(self) -> tuple[Decimal, str]:
        """Net worth across all LIVE accounts, e.g. ``(Decimal('97490.21'), 'EUR')``."""
        nw = self.get_accounts_overview().get("LIVE", {}).get("netWorth", {})
        return Decimal(str(nw.get("value", 0))), nw.get("currencyCode", self.currency)

    def get_account_summary(self, target_currency: str | None = None) -> dict:
        """``POST /rest/v1/equity/multi-accounts/summary`` (raw).

        Contains per trading type ``cash``, ``orders``, ``valueOrders``, ``open`` (positions).
        """
        return self._post("/rest/v1/equity/multi-accounts/summary", body=[],
                          params={"targetCurrency": target_currency or self.currency})

    def get_cash(self, target_currency: str | None = None) -> Cash:
        summary = self.get_account_summary(target_currency)
        by_type = summary.get("accountsByType", {})
        acc = by_type.get(self.trading_type) or next(iter(by_type.values()), {})
        currency = acc.get("accountCurrency") or summary.get("activeAccountCurrency") or self.currency
        return Cash.from_json(acc.get("cash", {}), currency)

    def get_wallets(self, currencies: list[str] | None = None) -> list[Wallet]:
        """``GET /rest/trading/v1/account/wallets`` – balance per currency."""
        params = {"currencyCodes": ",".join(currencies)} if currencies else None
        data = self._get("/rest/trading/v1/account/wallets", params=params)
        wallets = [Wallet.from_json(cur, w) for cur, w in data.items()]
        return sorted(wallets, key=lambda w: (not w.primary, w.currency))

    def get_budget(self, target_currency: str | None = None) -> Budget:
        """Budget = cash overview + wallets."""
        return Budget(account_id=self.account_id or 0,
                      cash=self.get_cash(target_currency),
                      wallets=self.get_wallets())

    # ------------------------------------------------------------------ market data / search
    def get_last_prices(self, tickers: list[str], extended: bool = True) -> dict[str, Decimal]:
        """Last traded price per ticker, e.g. ``{"INTC_US_EQ": Decimal("61.42")}``."""
        return self._post("/instrumentarium/v1/prices/last-trade",
                          body={"tickers": list(tickers), "extended": extended})

    def search_instruments(self, query: str, limit: int = 10) -> list[Instrument]:
        """Instrument search as in the web app (Trading212's Algolia index)."""
        cfg = self._get("/rest/instrument-search/v3/config",
                        params={"countryCode": (self.login_result.raw.get("residencyCode")
                                                if self.login_result else None) or "DE"})
        cred = cfg["credentials"]
        prof = cfg["profiles"]["search"]
        app_id = cred["applicationId"]
        url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"
        body = {"requests": [{
            "indexName": prof["indexName"],
            "query": query,
            "hitsPerPage": limit,
            "page": 0,
            "filters": prof.get("searchParams", {}).get("filters", ""),
            "attributesToRetrieve": ["objectID", "shortName", "name", "fullName", "category", "type",
                                     "exchangeName", "currencyCode", "isin", "state"],
        }]}
        headers = {"Accept": "application/json", "Content-Type": "text/plain",
                   "Origin": self.config.app_origin, "Referer": self.config.app_origin + "/",
                   "User-Agent": self.config.user_agent}
        try:
            resp = self.http.post(url, params={"x-algolia-application-id": app_id,
                                               "x-algolia-api-key": cred["searchApiKey"]},
                                  data=json.dumps(body), headers=headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise NetworkError(f"Instrument search failed: {exc}") from exc
        if not resp.ok:
            raise self._to_error(resp)
        hits = resp.json().get("results", [{}])[0].get("hits", [])
        return [Instrument.from_json(h) for h in hits]

    def resolve_ticker(self, symbol_or_ticker: str, *, market: str | None = None,
                       currency: str | None = None) -> Instrument:
        """``"INTC"``/``"Intel"``/``"US4581401001"``/``"INTC_US_EQ"`` → tradable instrument.

        * only instruments matching the account type (invest account → ``*_EQ``, no CFDs)
        * an exact ticker (``INTC_US_EQ``) is always used as given
        * otherwise, with several listings (e.g. NASDAQ in USD vs. Xetra in EUR),
          ``market`` / ``currency`` or ``config.preferred_market`` / ``preferred_currency``
          decide (default: US listing in USD)
        """
        explicit_market = market is not None and market != ""
        if currency is None:
            # explicitly another market (e.g. Xetra) → ignore the currency preference from the config
            currency = ("" if market and market.strip().upper() not in ("US", "NASDAQ", "NYSE")
                        else self.config.preferred_currency)
        market = self.config.preferred_market if market is None else market
        q = symbol_or_ticker.strip().upper()
        hits = self.search_instruments(symbol_or_ticker, limit=40)
        if "_" in q and not any(h.ticker.upper() == q for h in hits):
            hits += self.search_instruments(q.split("_")[0], limit=40)

        want = "CFD" if self.trading_type == "CFD" else ("CRYPTO" if self.trading_type == "CRYPTO" else "EQUITY")

        def fits(i: Instrument) -> bool:
            if not i.tradable:
                return False
            if want == "EQUITY":
                return i.category == "EQUITY" and i.ticker.endswith("_EQ")
            return i.category == want

        candidates = [i for i in hits if fits(i)]
        exact = [i for i in candidates if i.ticker.upper() == q]
        if exact:
            return exact[0]

        # relevance: ISIN > symbol > name; within equal relevance, market/currency decide
        def relevance(i: Instrument) -> int:
            if (i.isin or "").upper() == q:
                return 0
            # ticker stem without the lower-case exchange suffix: INLd_EQ → INL, VUAGl_EQ → VUAG
            stem = re.sub(r"[a-z]+$", "", i.ticker.split("_")[0]).upper()
            if i.short_name.upper() == q or stem == q:
                return 1
            if q in (i.name or "").upper() or q in (i.full_name or "").upper():
                return 2
            return 3

        aliases = {"XETRA": "XETRA", "XETR": "XETRA", "DE": "XETRA", "GER": "XETRA",
                   "NASDAQ": "NASDAQ", "NYSE": "NEW YORK STOCK EXCHANGE", "LSE": "LONDON",
                   "LONDON": "LONDON", "MILAN": "BORSA ITALIANA", "MAILAND": "BORSA ITALIANA",
                   "PARIS": "EURONEXT PARIS", "AMSTERDAM": "EURONEXT AMSTERDAM"}

        def market_score(i: Instrument) -> int:
            score = 0
            if market:
                m = market.strip().upper()
                exch = (i.exchange or "").upper()
                if m == "US":
                    score += 0 if "_US_" in i.ticker.upper() else 2
                elif aliases.get(m, m) not in exch:
                    score += 2
            if currency and (i.currency or "").upper() != currency.upper():
                score += 1
            return score

        if candidates:
            # Algolia's result order as the final tie-breaker
            ranked = sorted(enumerate(candidates), key=lambda t: (relevance(t[1]), market_score(t[1]), t[0]))
            best = ranked[0][1]
            if relevance(best) < 3:
                if explicit_market and market_score(best) >= 2:
                    listings = ", ".join(sorted({f"{i.ticker} ({i.exchange}, {i.currency})"
                                                 for i in candidates if relevance(i) == relevance(best)}))
                    raise ApiError(404, "InstrumentNotOnExchange", None, None, "",
                                   f"{symbol_or_ticker!r} is not tradable on {market!r}. "
                                   f"Available listings: {listings}")
                return best
        others = ", ".join(f"{h.ticker} ({h.category}, {h.exchange}, {h.currency})" for h in hits[:6])
        raise ApiError(404, "InstrumentNotFound", None, None, "",
                       f"No matching {want} instrument for {symbol_or_ticker!r}. Hits: {others or '-'}")

    # ------------------------------------------------------------------ orders
    def get_orders(self) -> list[Order]:
        """All open orders (quantity and value orders) of the active account."""
        summary = self.get_account_summary()
        acc = summary.get("accountsByType", {}).get(self.trading_type, {})
        raw = list(acc.get("orders", [])) + list(acc.get("valueOrders", []))
        return [Order.from_json(o) for o in raw]

    def get_instrument_settings(self, ticker: str, currency: str, order_type: str = "LIMIT",
                                target_price: Decimal | float | None = None) -> InstrumentSettings:
        body: dict[str, Any] = {"ticker": ticker, "currencyCode": currency, "orderType": order_type}
        if target_price is not None:
            body["targetPrice"] = _num(target_price)
        return InstrumentSettings.from_json(
            self._post("/rest/v1/equity/order/instruments/settings", body=body))

    @staticmethod
    def build_order(ticker: str, quantity: Decimal | float | int, *, side: str = "BUY",
                    order_type: str = "MARKET", currency: str,
                    limit_price: Decimal | float | None = None,
                    stop_price: Decimal | float | None = None,
                    time_validity: str = "DAY", extended_hours: bool = False) -> dict[str, Any]:
        """Build the request body for a quantity order.

        As in the web client, sell orders are encoded with a negative quantity.
        """
        side = side.upper()
        order_type = order_type.upper()
        time_validity = time_validity.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if order_type not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {ORDER_TYPES}")
        if time_validity not in TIME_VALIDITIES:
            raise ValueError(f"time_validity must be one of {TIME_VALIDITIES}")
        qty = Decimal(str(quantity))
        if qty <= 0:
            raise ValueError("quantity must be > 0 (direction is set via side)")
        if order_type in ("LIMIT", "STOP_LIMIT") and limit_price is None:
            raise ValueError(f"{order_type} requires limit_price")
        if order_type in ("STOP", "STOP_LIMIT") and stop_price is None:
            raise ValueError(f"{order_type} requires stop_price")

        body: dict[str, Any] = {
            "quantity": _num(-qty if side == "SELL" else qty),
            "instrumentCode": ticker,
            "currencyCode": currency,
            "orderType": order_type,
        }
        if limit_price is not None and order_type in ("LIMIT", "STOP_LIMIT"):
            body["limitPrice"] = _num(limit_price)
        if stop_price is not None and order_type in ("STOP", "STOP_LIMIT"):
            body["stopPrice"] = _num(stop_price)
        body["timeValidity"] = time_validity
        body["enabledExtendedMarketHours"] = bool(extended_hours)
        return body

    def preview_order(self, order: dict[str, Any]) -> OrderCosts:
        """Cost preview (``/rest/v2/public/added-costs``) – does not place anything."""
        return OrderCosts.from_json(self._post("/rest/v2/public/added-costs", body=order))

    def validate_order(self, order: dict[str, Any]) -> None:
        """Validate the order server-side; raises ``ApiError`` (e.g. ``InsufficientFreeForStocksBuy``,
        ``MarketClosed``, ``QuantityPrecisionMismatch``) if it would not be accepted."""
        self._require_login()
        self._request("POST", "/rest/public/v2/equity/order/validate",
                      headers=self._auth_headers(), json_body=order)

    def place_order(self, order: dict[str, Any], *, validate: bool = True) -> Order:
        """Place a quantity order. **Real money** in the LIVE account!

        Create ``order`` with :meth:`build_order`. Returns the newly created order.
        """
        if validate:
            self.validate_order(order)
        data = self._post("/rest/public/v2/equity/order", body=order)
        return self._pick_new_order(data, order)

    def place_value_order(self, ticker: str, value: Decimal | float, *, currency: str,
                          side: str = "BUY", time_validity: str = "DAY",
                          extended_hours: bool = False, validate: bool = True) -> Order:
        """Market order by value (e.g. buy for 500 USD). Sell = negative value."""
        body = self._value_order_body(ticker, value, currency, side, time_validity, extended_hours)
        if validate:
            self._require_login()
            self._request("POST", "/rest/v1/equity/value-order/validate",
                          headers=self._auth_headers(), json_body=body)
        data = self._post("/rest/v1/equity/value-order", body=body)
        return self._pick_new_order(data, body)

    def preview_value_order(self, ticker: str, value: Decimal | float, *, currency: str,
                            side: str = "BUY", time_validity: str = "DAY",
                            extended_hours: bool = False) -> OrderCosts:
        body = self._value_order_body(ticker, value, currency, side, time_validity, extended_hours)
        return OrderCosts.from_json(self._post("/rest/v1/equity/value-order/review", body=body))

    @staticmethod
    def _value_order_body(ticker, value, currency, side, time_validity, extended_hours) -> dict:
        v = Decimal(str(value))
        if v <= 0:
            raise ValueError("value must be > 0 (direction is set via side)")
        return {"value": _num(-v if side.upper() == "SELL" else v), "currency": currency,
                "instrumentCode": ticker, "orderType": "MARKET",
                "timeValidity": time_validity.upper(), "enabledExtendedMarketHours": bool(extended_hours)}

    def get_order_details(self, order_id: str | int) -> OrderCosts:
        """``GET /rest/v1/equity/order-details`` – quantity, value and costs of an open order."""
        return OrderCosts.from_json(self._get("/rest/v1/equity/order-details",
                                              params={"orderId": str(order_id)}))

    def get_order(self, order_id: str | int) -> Order | None:
        """Open order by id (None if no longer open)."""
        return next((o for o in self.get_orders() if o.order_id == str(order_id)), None)

    def cancel_order(self, order_id: str | int, *, value_order: bool | None = None,
                     wait: bool = False, timeout: float = 20.0, poll: float = 1.0) -> Order | None:
        """Cancel an open order (``DELETE /rest/public/v2/equity/order/{id}``).

        Trading212 cancels asynchronously: right afterwards the order is listed with status
        ``CANCELLING``. With ``wait=True`` the client polls until it has disappeared from
        the open orders (returns ``None``) – or until ``timeout``.

        ``value_order=None`` detects automatically whether it is a value order.
        Returns the order with its current status, or ``None`` if it is no longer open.
        """
        oid = str(order_id)
        if value_order is None:
            current = self.get_order(oid)
            if current is None:
                raise ApiError(404, "OrderNotFound", None, None, "",
                               f"No open order with id {oid}")
            value_order = current.quantity is None and current.value is not None

        path = "/rest/v1/equity/value-order/" if value_order else "/rest/public/v2/equity/order/"
        data = self._delete(f"{path}{oid}") or {}
        acc = data.get("account", data) if isinstance(data, dict) else {}
        raw = list(acc.get("equityOrders", [])) + list(acc.get("equityValueOrders", []))
        found = next((Order.from_json(o) for o in raw if str(o.get("orderId")) == oid), None)
        log.info("Order %s cancelled, status: %s", oid, found.status if found else "no longer open")

        if not wait or found is None:
            return found
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll)
            found = self.get_order(oid)
            if found is None or found.status not in ("CANCELLING", "NEW", "PENDING"):
                return found
        return found

    def cancel_orders(self, *, ticker: str | None = None, side: str | None = None,
                      wait: bool = False) -> list[tuple[Order, Order | None]]:
        """Cancel all open orders, optionally filtered by ticker and/or BUY/SELL.

        Returns a list of ``(original order, result of cancel_order)``.
        """
        result = []
        for o in self.get_orders():
            if ticker and o.ticker.upper() != ticker.upper():
                continue
            if side and o.side != side.upper():
                continue
            if o.status == "CANCELLING":
                continue
            result.append((o, self.cancel_order(o.order_id, value_order=o.quantity is None, wait=wait)))
        return result

    def settlement_currency(self, inst: Instrument, override: str | None = None) -> str:
        """Settlement currency (``currencyCode``) for an order on ``inst``.

        Default: the listing's trading currency – so US stocks settle in USD (proceeds go to
        the USD wallet, no FX fee), Xetra in EUR. ``override`` or
        ``config.settlement_currency = "EUR"`` force a fixed currency.
        """
        cur = (override or self.config.settlement_currency or "instrument").strip()
        if cur.lower() == "instrument":
            if inst.currency and inst.currency.upper() not in ("GBX", "GBP"):
                return inst.currency.upper()
            if inst.currency and inst.currency.upper() == "GBX":
                return "GBP"                      # London quotes in pence, settles in pounds
            if "_US_" in inst.ticker:
                return "USD"
            return self.currency
        return cur.upper()

    # convenience ------------------------------------------------------------
    def order(self, symbol: str, quantity, *, side: str = "BUY", limit_price=None, stop_price=None,
              time_validity: str = "DAY", exchange: str | None = None, currency: str | None = None,
              extended_hours: bool = False, validate: bool = True) -> Order:
        """Order by symbol/name/ISIN/ticker with exchange selection.

        Examples::

            t212.order("INTC", 2, limit_price=60)                       # NASDAQ, settles in USD
            t212.order("INTC", 60, side="SELL", limit_price=63)         # proceeds to the USD wallet
            t212.order("INTC", 60, side="SELL", limit_price=63, currency="EUR")  # with FX conversion
            t212.order("Intel", 2, limit_price=55, exchange="Xetra")    # Xetra, EUR
            t212.order("SIE", 5, side="SELL", limit_price=280, exchange="Xetra",
                       time_validity="GOOD_TILL_CANCEL")
        """
        inst = self.resolve_ticker(symbol, market=exchange)
        order_type = ("STOP_LIMIT" if limit_price is not None and stop_price is not None
                      else "LIMIT" if limit_price is not None
                      else "STOP" if stop_price is not None else "MARKET")
        body = self.build_order(inst.ticker, quantity, side=side, order_type=order_type,
                                currency=self.settlement_currency(inst, currency),
                                limit_price=limit_price, stop_price=stop_price,
                                time_validity=time_validity, extended_hours=extended_hours)
        return self.place_order(body, validate=validate)

    def buy_limit(self, ticker: str, quantity, limit_price, *, currency: str,
                  time_validity: str = "DAY", extended_hours: bool = False) -> Order:
        return self.place_order(self.build_order(ticker, quantity, side="BUY", order_type="LIMIT",
                                                 currency=currency, limit_price=limit_price,
                                                 time_validity=time_validity,
                                                 extended_hours=extended_hours))

    def sell_limit(self, ticker: str, quantity, limit_price, *, currency: str,
                   time_validity: str = "DAY", extended_hours: bool = False) -> Order:
        return self.place_order(self.build_order(ticker, quantity, side="SELL", order_type="LIMIT",
                                                 currency=currency, limit_price=limit_price,
                                                 time_validity=time_validity,
                                                 extended_hours=extended_hours))

    def buy_market(self, ticker: str, quantity, *, currency: str, extended_hours: bool = False) -> Order:
        return self.place_order(self.build_order(ticker, quantity, side="BUY", order_type="MARKET",
                                                 currency=currency, extended_hours=extended_hours))

    def sell_market(self, ticker: str, quantity, *, currency: str, extended_hours: bool = False) -> Order:
        return self.place_order(self.build_order(ticker, quantity, side="SELL", order_type="MARKET",
                                                 currency=currency, extended_hours=extended_hours))

    @staticmethod
    def _pick_new_order(data: dict, sent: dict) -> Order:
        """The response contains the whole account; find the order that was just created."""
        acc = data.get("account", data)
        candidates = list(acc.get("equityOrders", [])) + list(acc.get("equityValueOrders", []))
        ticker = sent["instrumentCode"]

        def same(a, b) -> bool:
            return a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))

        matches = [o for o in candidates if o.get("code") == ticker
                   and (same(o.get("quantity"), sent.get("quantity")) or same(o.get("value"), sent.get("value")))
                   and (sent.get("limitPrice") is None or same(o.get("limitPrice"), sent.get("limitPrice")))]
        if not matches:
            matches = [o for o in candidates if o.get("code") == ticker]
        if not matches:
            raise ApiError(200, "OrderNotFoundInResponse", None, None, "",
                           "Order sent but not found in the response – please check your orders.")
        matches.sort(key=lambda o: o.get("created") or "", reverse=True)
        return Order.from_json(matches[0])


def _num(v: Any) -> int | float:
    """Number for JSON: integers as int, otherwise float (as in the web client)."""
    d = Decimal(str(v))
    return int(d) if d == d.to_integral_value() else float(d)
