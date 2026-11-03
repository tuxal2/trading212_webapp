"""One-time login through a browser window using a stealth browser (Playwright + playwright-stealth).

The web app's password login is protected by Cloudflare Turnstile. The user logs in
in the window themselves (including captcha and 2FA). The script captures the login
response, cookies and device id and stores them in the session file. After that
everything runs without a browser via ``/rest/v4/login/token`` or the stored session.

With ``username``/``password`` the form is filled automatically (also headless). The
2FA code comes from ``otp`` or ``otp_callback``. If Cloudflare shows an interactive
challenge instead of passing silently, the automatic login gives up – then run it
again with a visible window.

Stealth measures:
  * playwright-stealth (navigator.webdriver, plugins, WebGL, sec-ch-ua, ...)
  * no ``--enable-automation``, plus ``--disable-blink-features=AutomationControlled``
  * prefers the installed Google Chrome over Playwright's Chromium
  * persistent browser profile so Cloudflare cookies are kept

Requirements:
    pip install playwright playwright-stealth
    playwright install chromium      # only needed if Google Chrome is not installed
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .exceptions import Trading212Error

if TYPE_CHECKING:
    from .client import Trading212Client

log = logging.getLogger(__name__)

LOGIN_PATHS = ("/rest/v5/login", "/rest/v4/login/token", "/rest/v2/webclient/authenticate",
               "/rest/v3/webclient/authenticate")

DEFAULT_PROFILE_DIR = Path("~/.config/trading212/browser-profile").expanduser()


def apply_browser_capture(client: "Trading212Client", login_json: dict,
                          request_headers: dict[str, str], cookies: list[dict]) -> None:
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


def _launch(pw, profile_dir: Path | None, headless: bool, channel: str | None):
    """Start a persistent context; try Google Chrome first, then Chromium."""
    kwargs = dict(
        headless=headless,
        locale="de-DE",
        timezone_id="Europe/Berlin",
        viewport=None if not headless else {"width": 1366, "height": 860},
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        ignore_default_args=["--enable-automation"],
    )
    channels = [channel] if channel else ["chrome", None]
    last_exc: Exception | None = None
    for ch in channels:
        try:
            opts = dict(kwargs, **({"channel": ch} if ch else {}))
            if profile_dir:
                profile_dir.mkdir(parents=True, exist_ok=True)
                return pw.chromium.launch_persistent_context(str(profile_dir), **opts)
            browser = pw.chromium.launch(**{k: v for k, v in opts.items()
                                            if k in ("headless", "args", "ignore_default_args", "channel")})
            return browser.new_context(**{k: v for k, v in opts.items()
                                          if k in ("locale", "timezone_id", "viewport")})
        except Exception as exc:  # Chrome not installed or similar
            log.info("Browser start with channel=%s failed: %s", ch, exc)
            last_exc = exc
    raise Trading212Error(f"Could not start a browser ({last_exc}). "
                          "Install Google Chrome or run 'playwright install chromium'.")


SEL_EMAIL = '[data-testid="login-form-email-input"]'
SEL_PASSWORD = '[data-testid="login-form-password-input"]'
SEL_SUBMIT = '[data-testid="login-form-log-in-button"]'
CF_CHALLENGE = "challenges.cloudflare.com"
CF_BLOCK_TEXTS = ("Access Denied", "You do not have access to", "Sorry, you have been blocked")
DEBUG_SCREENSHOT = DEFAULT_PROFILE_DIR.parent / "browser-login-debug.png"


def _is_login_url(url: str) -> bool:
    return any(url.split("?")[0].endswith(p) for p in LOGIN_PATHS)


def _error_type(resp) -> str | None:
    try:
        data = resp.json()
    except Exception:
        return None
    return (data.get("context") or {}).get("type") or data.get("code")


def _visible_cf_challenge(page) -> bool:
    """True if a Cloudflare challenge iframe is visible (interactive check)."""
    for frame in page.frames:
        if CF_CHALLENGE in frame.url:
            try:
                el = frame.frame_element()
                if el.is_visible() and (el.bounding_box() or {}).get("height", 0) > 10:
                    return True
            except Exception:
                pass
    return False


def _cf_blocked(page) -> bool:
    """True if Cloudflare shows its block page instead of the web app."""
    try:
        text = page.locator("body").inner_text(timeout=1000)
    except Exception:
        return False
    return any(t in text for t in CF_BLOCK_TEXTS)


def _save_screenshot(page) -> None:
    try:
        page.screenshot(path=str(DEBUG_SCREENSHOT), timeout=5000)
        print(f"Screenshot of the browser: {DEBUG_SCREENSHOT}", file=sys.stderr)
    except Exception as exc:
        log.debug("Screenshot failed: %s", exc)


def _enter_otp(page, code: str, timeout_ms: int = 20000) -> None:
    """Type the 2FA code into the first visible input that is not the login form."""
    sel = f"input:visible:not({SEL_EMAIL}):not({SEL_PASSWORD})"
    page.wait_for_selector(sel, timeout=timeout_ms)
    page.locator(sel).first.click()
    page.keyboard.type(code, delay=60)
    page.keyboard.press("Enter")


def _auto_login(page, url: str, username: str, password: str, otp: str | None,
                otp_callback: Callable[[dict], str] | None, timeout_s: int):
    """Fill the login form and wait for the successful login response."""
    events: list = []
    page.on("response", lambda r: events.append(r) if _is_login_url(r.url) else None)
    page.goto(url, wait_until="domcontentloaded")

    deadline = time.monotonic() + timeout_s
    form_sent = otp_sent = False
    while time.monotonic() < deadline:
        if _cf_blocked(page):
            raise Trading212Error("Cloudflare blocks this browser ('Access Denied'). Headless login is not "
                                  "possible right now – run browser-login without --headless, or wait "
                                  "and try again later.")
        # the persistent profile may log in by itself – then there is no form
        if not form_sent and page.locator(SEL_EMAIL).is_visible():
            log.debug("Login form visible, submitting")
            page.fill(SEL_EMAIL, username)
            page.fill(SEL_PASSWORD, password)
            page.click(SEL_SUBMIT)
            form_sent = True
        while events:
            resp = events.pop(0)
            log.debug("Login response %s %s", resp.status, resp.url)
            if resp.status == 200:
                return resp
            err = _error_type(resp)
            if not form_sent:
                continue                 # session probe of the web app before login (401 InvalidSession)
            if err == "TwoFactorAuthenticationRequired" and not otp_sent:
                ctx = {}
                try:
                    ctx = resp.json().get("context") or {}
                except Exception:
                    pass
                code = otp or (otp_callback(ctx) if otp_callback else None)
                if not code:
                    raise Trading212Error("2FA code required (--otp or $T212_OTP).")
                _enter_otp(page, str(code).strip())
                otp_sent = True
            elif err == "TwoFactorAuthenticationRequired":
                raise Trading212Error("2FA code was rejected.")
            else:
                raise Trading212Error(f"Login failed: {err or resp.status} ({resp.url})")
        page.wait_for_timeout(250)

    if _visible_cf_challenge(page):
        raise Trading212Error("Cloudflare shows an interactive challenge – "
                              "run browser-login without --headless and log in in the window.")
    raise Trading212Error(f"No login response within {timeout_s} s "
                          f"({'form submitted' if form_sent else 'login form never appeared'}).")


def browser_login(client: "Trading212Client", timeout_s: int = 300, headless: bool = False,
                  profile_dir: Path | None = DEFAULT_PROFILE_DIR, channel: str | None = None,
                  username: str | None = None, password: str | None = None, otp: str | None = None,
                  otp_callback: Callable[[dict], str] | None = None) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise Trading212Error("Playwright is missing: pip install playwright playwright-stealth") from exc

    auto = bool(username and password)
    if headless and not auto:
        raise Trading212Error("Headless browser login needs username and password.")

    stealth = _make_stealth()

    with stealth.use_sync(sync_playwright()) as pw:
        context = _launch(pw, profile_dir, headless, channel)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if auto:
                print("Logging in automatically ...")
                try:
                    resp = _auto_login(page, client.config.app_origin + "/", username, password,
                                       otp, otp_callback, timeout_s)
                except (Exception, KeyboardInterrupt):
                    _save_screenshot(page)
                    raise
            else:
                print(f"Please log in in the browser window (including 2FA). Waiting up to {timeout_s} s ...")
                with page.expect_response(lambda r: r.status == 200 and _is_login_url(r.url),
                                          timeout=timeout_s * 1000) as info:
                    page.goto(client.config.app_origin + "/", wait_until="domcontentloaded")
                resp = info.value
            data = resp.json()
            if "accountSession" not in data:
                raise Trading212Error(f"Unexpected login response from {resp.url}")
            headers = resp.request.all_headers()
            page.wait_for_timeout(1500)          # the web app sets cookies via JS
            cookies = context.cookies()
        finally:
            try:
                context.close()
            except Exception as exc:     # driver already gone (e.g. after Ctrl-C)
                log.debug("Closing the browser failed: %s", exc)

    apply_browser_capture(client, data, headers, cookies)
    log.info("Browser login captured: account %s", client.account_id)
