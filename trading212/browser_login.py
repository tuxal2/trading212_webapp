"""Login through a browser window (Playwright + playwright-stealth).

The web app's password login is protected by Cloudflare Turnstile. The user logs in
in the window themselves (including captcha and 2FA). The script captures the login
response, cookies and device id and stores them in the session file. After that
everything runs without a browser: an expired session is renewed with the
``LOGIN_TOKEN`` cookie (``/rest/v3/webclient/authenticate``).

The login requests get ``rememberMe`` / ``rememberDevice`` from the config, so the
device is trusted and the renewal needs no 2FA.

Browser setup:
  * playwright-stealth (navigator.webdriver, plugins, WebGL, sec-ch-ua, ...)
  * no ``--enable-automation``, plus ``--disable-blink-features=AutomationControlled``
  * extensions allowed: install e.g. your password manager once in the login window – it stays
    in the login profile
  * prefers the installed Google Chrome over Playwright's Chromium; Microsoft Edge, Brave
    (``--browser-channel brave``) or any other Chromium-based browser (``--browser-path``)
    can be chosen instead
  * or Camoufox (``--browser-channel camoufox``): a Firefox build with a consistent,
    human-looking fingerprint, often passes Cloudflare more smoothly; own profile folder
    ``<browser-profile>-camoufox``. Install: ``pip install -U "camoufox[geoip]"`` and
    ``python -m camoufox fetch``. Chrome extensions do not run there.
  * persistent browser profile so Cloudflare cookies are kept

Requirements:
    pip install playwright playwright-stealth
    playwright install chromium      # only needed if Google Chrome is not installed
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import Trading212Error
from .totp import totp

if TYPE_CHECKING:
    from .client import Trading212Client

log = logging.getLogger(__name__)

LOGIN_PATHS = ("/rest/v5/login", "/rest/v4/login/token", "/rest/v2/webclient/authenticate",
               "/rest/v3/webclient/authenticate")

DEFAULT_PROFILE_DIR = Path("~/.config/trading212/browser-profile").expanduser()

# fields of the web app's login form (with generic fallbacks)
EMAIL_FIELD = '[data-testid="login-form-email-input"], input[type="email"], input[autocomplete="email"]'
PASSWORD_FIELD = '[data-testid="login-form-password-input"], input[type="password"]'
PREFILL_WAIT_MS = 20_000
TURNSTILE_FIELD = 'input[name="cf-turnstile-response"]'
# Trading212: six boxes data-testid="2fa-digit-input" (only the first has autocomplete=one-time-code)
OTP_FIELD = ('input[data-testid="2fa-digit-input"], input[data-testid*="2fa" i], input[autocomplete="one-time-code"], '
             'input[data-testid*="code" i], input[name*="code" i], input[data-testid*="otp" i], '
             'input[inputmode="numeric"]')
SUBMIT_NAME = re.compile(r"^\s*(log ?in|sign in|anmelden|einloggen)\s*$", re.I)
CONFIRM_NAME = re.compile(r"^\s*(confirm|verify|continue|submit|log ?in|fortfahren|bestätigen|weiter|anmelden)\s*$",
                          re.I)
TURNSTILE_WAIT_MS = 120_000
OTP_WAIT_MS = 60_000

# Chromium-based browsers Playwright has no channel for: started by their executable.
BROWSER_EXECUTABLES: dict[str, list[str]] = {
    "brave": [
        "brave-browser", "brave", "brave-browser-stable",                     # Linux (PATH)
        "/opt/brave.com/brave/brave", "/snap/bin/brave",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",       # macOS
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",  # Windows
        r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
}
PLAYWRIGHT_CHANNELS = ("chrome", "chrome-beta", "msedge", "msedge-beta", "msedge-dev", "chromium")


def find_executable(name: str) -> str | None:
    """Path of a known browser (``brave``) – on PATH or in its usual install location."""
    for cand in BROWSER_EXECUTABLES.get(name.lower(), []):
        cand = os.path.expandvars(cand)
        if os.sep not in cand and "/" not in cand:
            hit = shutil.which(cand)
            if hit:
                return hit
        elif Path(cand).is_file():
            return cand
    return None


def resolve_browser(channel: str | None, path: str | None = None) -> tuple[str | None, str | None]:
    """``(playwright channel, executable path)`` for the options ``--browser-channel`` / ``--browser-path``.

    ``path`` (or ``$T212_BROWSER_PATH``) wins. ``brave`` is looked up on disk; the Playwright
    channels (``chrome``, ``msedge``, ``chromium`` …) are passed through.
    """
    path = path or os.environ.get("T212_BROWSER_PATH") or None
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise Trading212Error(f"Browser not found: {p}")
        return None, str(p)
    if not channel:
        return None, None
    ch = channel.strip().lower()
    if ch == "camoufox":
        return "camoufox", None
    if ch in BROWSER_EXECUTABLES:
        exe = find_executable(ch)
        if not exe:
            raise Trading212Error(f"{channel} is not installed (or not found) – pass its program file "
                                  "with --browser-path")
        return None, exe
    if ch not in PLAYWRIGHT_CHANNELS:
        raise Trading212Error(f"Unknown browser {channel!r} – use one of "
                              f"{', '.join(PLAYWRIGHT_CHANNELS + tuple(BROWSER_EXECUTABLES) + ('camoufox',))} "
                              "or --browser-path")
    return ch, None


def apply_browser_capture(client: "Trading212Client", login_json: dict,
                          request_headers: dict[str, str], cookies: list[dict],
                          request_body: dict | None = None) -> None:
    """Transfer the login response captured in the browser to the client."""
    trader_client = next((v for k, v in request_headers.items() if k.lower() == "x-trader-client"), "")
    m = re.search(r"dUUID=([0-9a-fA-F-]{36})", trader_client)
    if m:
        client.device_id = m.group(1)
    ua = next((v for k, v in request_headers.items() if k.lower() == "user-agent"), None)
    if ua:
        client.config.user_agent = ua

    for c in cookies:
        if c.get("domain", "").lstrip(".").endswith("trading212.com"):
            client.http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))

    client._apply_login(login_json)
    # token login (/rest/v4/login/token) does not return a new token – keep the one that was sent
    if not login_json.get("loginToken") and (request_body or {}).get("token"):
        client._login_token = request_body["token"]
    if not client.config.username:
        client.config.username = login_json.get("email")
    client._save_session_file()


def _platform_override() -> str:
    if sys.platform.startswith("win"):
        return "Win32"
    if sys.platform == "darwin":
        return "MacIntel"
    return "Linux x86_64"


def _make_stealth():
    try:
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise Trading212Error("playwright-stealth is missing: pip install playwright-stealth") from exc
    return Stealth(
        navigator_languages_override=("de-DE", "de"),
        navigator_platform_override=_platform_override(),
    )


def parse_headless(value) -> bool | str:
    """``None``/``off`` → window, ``on``/``true`` → headless, ``virtual`` → virtual display (Camoufox, xvfb)."""
    v = str(value).strip().lower() if value is not None else "off"
    if v in ("", "off", "false", "0", "no", "window"):
        return False
    if v in ("on", "true", "1", "yes", "headless"):
        return True
    if v == "virtual":
        return "virtual"
    raise Trading212Error(f"--headless must be 'on', 'virtual' or 'off', not {value!r}")


def _launch(pw, profile_dir: Path | None, channel: str | None, executable_path: str | None = None,
            headless: bool = False):
    """Start a persistent context with a window.

    Default: Google Chrome first, then Playwright's Chromium. ``channel`` picks one Playwright
    channel; ``executable_path`` starts that Chromium-based browser (e.g. Brave).
    """
    kwargs = dict(
        headless=bool(headless),
        locale="de-DE",
        timezone_id="Europe/Berlin",
        viewport=None,
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        # Playwright starts Chromium with these; drop them so the login window behaves like a normal
        # browser and extensions installed in the login profile (e.g. a password manager) load.
        ignore_default_args=["--enable-automation", "--disable-extensions",
                             "--disable-component-extensions-with-background-pages"],
    )
    if executable_path:
        attempts = [{"executable_path": executable_path}]
    else:
        channels = [channel] if channel else ["chrome", None]
        attempts = [{"channel": ch} if ch and ch != "chromium" else {} for ch in channels]
    last_exc: Exception | None = None
    for extra in attempts:
        try:
            opts = dict(kwargs, **extra)
            if profile_dir:
                profile_dir.mkdir(parents=True, exist_ok=True)
                return pw.chromium.launch_persistent_context(str(profile_dir), **opts)
            browser = pw.chromium.launch(**{k: v for k, v in opts.items()
                                            if k in ("headless", "args", "ignore_default_args", "channel",
                                                     "executable_path")})
            return browser.new_context(**{k: v for k, v in opts.items()
                                          if k in ("locale", "timezone_id", "viewport")})
        except Exception as exc:  # browser not installed or similar
            log.info("Browser start with %s failed: %s", extra or "bundled chromium", exc)
            last_exc = exc
    raise Trading212Error(f"Could not start a browser ({last_exc}). "
                          "Install Google Chrome, run 'playwright install chromium' or pass --browser-path.")


def _is_login_url(url: str) -> bool:
    return any(url.split("?")[0].endswith(p) for p in LOGIN_PATHS)


def _request_json(req) -> dict | None:
    try:
        data = json.loads(req.post_data) if req.post_data else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _route_login_options(context, remember_me: bool, remember_device: bool) -> None:
    """Send the config's rememberMe / rememberDevice with the web app's login requests."""
    def handle(route):
        req = route.request
        body = _request_json(req) if req.method == "POST" and _is_login_url(req.url) else None
        if body is None:
            return route.continue_()
        if "rememberMe" in body:
            body["rememberMe"] = remember_me
        if isinstance(body.get("twoFactorAuth"), dict):
            body["twoFactorAuth"]["rememberDevice"] = remember_device
        route.continue_(post_data=json.dumps(body))
    context.route(re.compile(r".*/rest/.*"), handle)


def browser_login(client: "Trading212Client", timeout_s: int = 300,
                  profile_dir: Path | None = DEFAULT_PROFILE_DIR, channel: str | None = None,
                  executable_path: str | None = None, prefill: bool = True, auto_submit: bool = True,
                  debug_dir: Path | str | None = None, headless: bool | str | None = None) -> None:
    """Open the web app in a window, wait for the user's login and store the session.

    ``channel``: Playwright channel (``chrome``, ``msedge``, ``chromium``) or ``brave``;
    ``executable_path``: any Chromium-based browser (wins over ``channel``).
    ``prefill``: fill in ``config.username`` / ``config.password``; ``auto_submit``: then also submit
    the form (after the Cloudflare check) and enter the 2FA code from ``config.totp_secret``.
    ``debug_dir``: screenshots + form structure per step (never field values).
    ``headless``: ``"on"`` (no window – Cloudflare may refuse), ``"virtual"`` (Camoufox on a virtual
    display, needs xvfb) or ``None``/``"off"`` (window). Without a window the login must run
    fully automatically (e-mail, password and totp_secret configured).
    """
    channel, executable_path = resolve_browser(channel, executable_path)
    dbg = Debugger(Path(debug_dir) if debug_dir else None)
    hl = parse_headless(headless if headless is not None else os.environ.get("T212_HEADLESS"))
    if hl == "virtual" and channel != "camoufox":
        raise Trading212Error("--headless virtual needs --browser-channel camoufox (use --headless on otherwise)")
    if hl:
        cfg = client.config
        if not (cfg.username and cfg.password and cfg.totp_secret) or not prefill or not auto_submit:
            raise Trading212Error("Without a window the login must run automatically: set username, "
                                  "password (or password_file) and totp_secret, and do not use "
                                  "--no-prefill/--no-submit")
        print("Logging in without a window ...")
    if channel == "camoufox":
        data, headers, cookies, body = _login_camoufox(client, timeout_s, profile_dir, prefill, auto_submit, dbg, hl)
    else:
        data, headers, cookies, body = _login_chromium(client, timeout_s, profile_dir, channel, executable_path,
                                                       prefill, auto_submit, dbg, bool(hl))
    apply_browser_capture(client, data, headers, cookies, body)
    log.info("Browser login captured: account %s", client.account_id)


def _prefill(page, username: str | None, password: str | None) -> bool:
    """Fill e-mail and password into the login form – it is NOT submitted.

    The Cloudflare check, the click on "Log in" and the 2FA code stay with the user. Fields that
    already contain something (e.g. from a password manager) are left alone. Returns True if filled.
    """
    if not username and not password:
        return False
    try:
        email = page.locator(EMAIL_FIELD).first
        email.wait_for(state="visible", timeout=PREFILL_WAIT_MS)
        if username and not email.input_value():
            email.fill(username)
        if password:
            pw = page.locator(PASSWORD_FIELD).first
            pw.wait_for(state="visible", timeout=5_000)
            if not pw.input_value():
                pw.fill(password)
        return True
    except Exception as exc:  # noqa: BLE001 - no form (e.g. still logged in) → user does it
        log.info("Login form not pre-filled: %s", exc)
        return False


# what the debug dump records about the page – structure only, never field values
_DUMP_JS = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const attrs = e => ({tag: e.tagName.toLowerCase(), type: e.type || null, name: e.name || null, id: e.id || null,
    testid: e.getAttribute('data-testid') || e.getAttribute('data-qa') || null,
    autocomplete: e.getAttribute('autocomplete'), inputmode: e.getAttribute('inputmode'),
    maxlength: e.getAttribute('maxlength'), placeholder: e.getAttribute('placeholder'),
    aria: e.getAttribute('aria-label'), visible: vis(e), filled: !!(e.value && e.value.length)});
  return {
    url: location.origin + location.pathname,
    title: document.title,
    inputs: [...document.querySelectorAll('input,textarea,select')].map(attrs),
    buttons: [...document.querySelectorAll('button,[role=button],input[type=submit]')].map(b => ({
      text: (b.innerText || b.value || '').trim().slice(0, 60), type: b.type || null,
      testid: b.getAttribute('data-testid') || null, disabled: !!b.disabled, visible: vis(b)})),
    iframes: [...document.querySelectorAll('iframe')].map(f => (f.src || '').split('?')[0]),
    headings: [...document.querySelectorAll('h1,h2,h3')].map(h => h.innerText.trim().slice(0, 80)).filter(Boolean),
  };
}"""


class Debugger:
    """``--debug-dir``: per step a screenshot and a JSON list of the form fields (no values)."""

    def __init__(self, directory: Path | None):
        self.dir = Path(directory).expanduser() if directory else None
        self.n = 0
        self.problems: list[str] = []         # failed / refused requests and console errors (no queries)
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            print(f"Debug output goes to {self.dir}")

    def attach(self, page) -> None:
        """Record failed requests, HTTP errors and console errors – shows e.g. Cloudflare refusals."""
        if not self.dir:
            return

        def short(url: str) -> str:
            return url.split("?")[0][:140]

        def add(text: str) -> None:
            self.problems.append(f"{time.strftime('%H:%M:%S')} {text}")
            del self.problems[:-60]

        page.on("requestfailed", lambda r: add(f"FAILED {r.method} {short(r.url)} {r.failure or ''}"))
        page.on("response", lambda r: add(f"HTTP {r.status} {r.request.method} {short(r.url)}")
                if r.status >= 400 else None)
        page.on("console", lambda m: add(f"console.{m.type} {m.text[:200]}") if m.type in ("error", "warning")
                else None)

    def dump(self, page, step: str) -> None:
        if not self.dir:
            return
        self.n += 1
        base = self.dir / f"{self.n:02d}-{re.sub(r'[^a-z0-9]+', '-', step.lower()).strip('-')}"
        try:
            info = page.evaluate(_DUMP_JS)
            info["step"] = step
            info["problems"] = list(self.problems)
            base.with_suffix(".json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.info("debug dump of %s failed: %s", step, exc)
        try:
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception as exc:  # noqa: BLE001
            log.info("debug screenshot of %s failed: %s", step, exc)


NO_DEBUG = Debugger(None)


def _click_or_enter(page, name: re.Pattern, field) -> None:
    btn = page.get_by_role("button", name=name)
    if btn.count():
        btn.first.click()
    else:
        field.press("Enter")


def _wait_turnstile(page) -> None:
    """If the Cloudflare widget is on the page, wait for its token (click it yourself if it asks)."""
    if page.locator(TURNSTILE_FIELD).count() == 0:
        return
    print("Waiting for the Cloudflare check – click it in the window if it asks you to ...")
    page.wait_for_function(
        "() => [...document.querySelectorAll('input[name=\"cf-turnstile-response\"]')].some(i => i.value)",
        timeout=TURNSTILE_WAIT_MS)


def _fill_otp(page, secret: str) -> None:
    """Type the current TOTP code into the 2FA field(s) and confirm."""
    fields = [f for f in page.locator(OTP_FIELD).all() if f.is_visible()]
    if not fields:
        raise Trading212Error("no 2FA field")
    if 30 - (time.time() % 30) < 3:          # code about to change – take the next one
        page.wait_for_timeout(3500)
    code = totp(secret)
    if len(fields) >= len(code):             # one box per digit (Trading212: six boxes)
        for digit, f in zip(code, fields):
            f.fill(digit)
        last = fields[len(code) - 1]
    else:
        fields[0].fill(code)
        last = fields[0]
    page.wait_for_timeout(300)               # the "continue" button is enabled once all boxes are filled
    _click_or_enter(page, CONFIRM_NAME, last)


def _auto_login(page, cfg, done: dict, dbg: Debugger = NO_DEBUG) -> None:
    """Submit the pre-filled form and, with ``totp_secret``, enter the 2FA code – best effort.

    Anything that does not work out (e.g. an interactive Cloudflare check that is not solved,
    an unknown 2FA form) just leaves the window to the user.
    """
    try:
        _wait_turnstile(page)
        dbg.dump(page, "after cloudflare check")
        if done["v"]:
            return
        _click_or_enter(page, SUBMIT_NAME, page.locator(PASSWORD_FIELD).first)
        print("Login form submitted.")
        page.wait_for_timeout(2500)
        dbg.dump(page, "after submit")
        if not cfg.totp_secret:
            print("No totp_secret configured – enter the 2FA code in the window.")
            return
        deadline = time.monotonic() + OTP_WAIT_MS / 1000
        while time.monotonic() < deadline and not done["v"]:
            if any(f.is_visible() for f in page.locator(OTP_FIELD).all()):
                dbg.dump(page, "2fa form")
                _fill_otp(page, cfg.totp_secret)
                print("2FA code entered.")
                page.wait_for_timeout(2500)
                dbg.dump(page, "after 2fa")
                return
            page.wait_for_timeout(500)
        if not done["v"]:
            print("No 2FA field recognised – enter the code in the window.")
            dbg.dump(page, "no 2fa field found")
    except Exception as exc:  # noqa: BLE001
        log.info("Automatic login step failed: %s", exc)
        print(f"Please complete the login in the window ({type(exc).__name__}).")
        dbg.dump(page, f"failed {type(exc).__name__}")


def _capture(context, client: "Trading212Client", timeout_s: int, prefill: bool = True,
             auto_submit: bool = True, dbg: Debugger = NO_DEBUG):
    """Open the web app in ``context`` and wait for the user's login → (json, headers, cookies, body)."""
    _route_login_options(context, client.config.remember_me, client.config.remember_device)
    page = context.pages[0] if context.pages else context.new_page()
    print(f"Please log in in the browser window (including 2FA). Waiting up to {timeout_s} s ...")
    done = {"v": False}                  # login response seen (also when the profile is still logged in)
    page.on("response", lambda r: done.update(v=True) if r.status == 200 and _is_login_url(r.url) else None)
    dbg.attach(page)
    try:
        with page.expect_response(lambda r: r.status == 200 and _is_login_url(r.url),
                                  timeout=timeout_s * 1000) as info:
            page.goto(client.config.app_origin + "/", wait_until="domcontentloaded")
            dbg.dump(page, "start")
            cfg = client.config
            if prefill and not done["v"]:
                if _prefill(page, cfg.username, cfg.password):
                    dbg.dump(page, "prefilled")
                    if auto_submit and cfg.username and cfg.password:
                        _auto_login(page, cfg, done, dbg)
                    else:
                        print("E-mail and password are filled in – complete the check, click 'Log in' and "
                              "enter the 2FA code.")
                elif not done["v"] and (cfg.username or cfg.password):
                    print(f"No login form after {PREFILL_WAIT_MS // 1000} s – the page did not load "
                          "(blocked?). Waiting for a login anyway ...")
                    dbg.dump(page, "no login form")
    except Exception as exc:
        dbg.dump(page, f"gave up {type(exc).__name__}")
        if "Timeout" in type(exc).__name__:
            raise Trading212Error(f"No login within {timeout_s} s"
                                  + (f" – see {dbg.dir}" if dbg.dir else "")) from exc
        raise
    resp = info.value
    data = resp.json()
    if "accountSession" not in data:
        raise Trading212Error(f"Unexpected login response from {resp.url}")
    headers = resp.request.all_headers()
    body = _request_json(resp.request)
    log.debug("Login captured via %s", resp.url.split("?")[0])
    page.wait_for_timeout(1500)          # the web app sets cookies via JS
    dbg.dump(page, "logged in")
    return data, headers, context.cookies(), body


def _close(context) -> None:
    try:
        context.close()
    except Exception as exc:     # driver already gone (e.g. after Ctrl-C)
        log.debug("Closing the browser failed: %s", exc)


def _login_chromium(client, timeout_s, profile_dir, channel, executable_path, prefill=True, auto_submit=True,
                    dbg=NO_DEBUG, headless=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise Trading212Error("Playwright is missing: pip install playwright playwright-stealth") from exc
    stealth = _make_stealth()
    with stealth.use_sync(sync_playwright()) as pw:
        context = _launch(pw, profile_dir, channel, executable_path, headless)
        try:
            return _capture(context, client, timeout_s, prefill, auto_submit, dbg)
        finally:
            _close(context)


def camoufox_options(profile_dir: Path | None, geoip: bool = True, headless: bool | str = False) -> dict:
    """Options for ``Camoufox(...)``: window (or headless / virtual display), human-like input,
    German locale, own profile."""
    opts: dict = {"headless": headless, "humanize": True, "locale": "de-DE", "geoip": geoip}
    if profile_dir:
        cf_dir = profile_dir.with_name(profile_dir.name + "-camoufox")
        cf_dir.mkdir(parents=True, exist_ok=True)
        opts.update(persistent_context=True, user_data_dir=str(cf_dir))
    return opts


def _login_camoufox(client, timeout_s, profile_dir, prefill=True, auto_submit=True, dbg=NO_DEBUG,
                    headless: bool | str = False):
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise Trading212Error('Camoufox is missing: pip install -U "camoufox[geoip]" && '
                              "python -m camoufox fetch") from exc
    last: Exception | None = None
    for geoip in (True, False):          # geoip needs the [geoip] extra and a network lookup
        try:
            cm = Camoufox(**camoufox_options(profile_dir, geoip, headless))
            opened = cm.__enter__()
        except Exception as exc:  # noqa: BLE001
            log.info("Camoufox start (geoip=%s) failed: %s", geoip, exc)
            last = exc
            continue
        try:
            context = opened if profile_dir else opened.new_context()
            try:
                return _capture(context, client, timeout_s, prefill, auto_submit, dbg)
            finally:
                if not profile_dir:
                    _close(context)
        finally:
            cm.__exit__(None, None, None)
    raise Trading212Error(f"Could not start Camoufox ({last}). Run 'python -m camoufox fetch' once.")
