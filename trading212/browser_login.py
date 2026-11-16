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
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import Trading212Error

if TYPE_CHECKING:
    from .client import Trading212Client

log = logging.getLogger(__name__)

LOGIN_PATHS = ("/rest/v5/login", "/rest/v4/login/token", "/rest/v2/webclient/authenticate",
               "/rest/v3/webclient/authenticate")

DEFAULT_PROFILE_DIR = Path("~/.config/trading212/browser-profile").expanduser()

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
    if ch in BROWSER_EXECUTABLES:
        exe = find_executable(ch)
        if not exe:
            raise Trading212Error(f"{channel} is not installed (or not found) – pass its program file "
                                  "with --browser-path")
        return None, exe
    if ch not in PLAYWRIGHT_CHANNELS:
        raise Trading212Error(f"Unknown browser {channel!r} – use one of "
                              f"{', '.join(PLAYWRIGHT_CHANNELS + tuple(BROWSER_EXECUTABLES))} or --browser-path")
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


def _launch(pw, profile_dir: Path | None, channel: str | None, executable_path: str | None = None):
    """Start a persistent context with a window.

    Default: Google Chrome first, then Playwright's Chromium. ``channel`` picks one Playwright
    channel; ``executable_path`` starts that Chromium-based browser (e.g. Brave).
    """
    kwargs = dict(
        headless=False,
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
                  executable_path: str | None = None) -> None:
    """Open the web app in a window, wait for the user's login and store the session.

    ``channel``: Playwright channel (``chrome``, ``msedge``, ``chromium``) or ``brave``;
    ``executable_path``: any Chromium-based browser (wins over ``channel``).
    """
    channel, executable_path = resolve_browser(channel, executable_path)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise Trading212Error("Playwright is missing: pip install playwright playwright-stealth") from exc

    stealth = _make_stealth()

    with stealth.use_sync(sync_playwright()) as pw:
        context = _launch(pw, profile_dir, channel, executable_path)
        try:
            _route_login_options(context, client.config.remember_me, client.config.remember_device)
            page = context.pages[0] if context.pages else context.new_page()
            print(f"Please log in in the browser window (including 2FA). Waiting up to {timeout_s} s ...")
            with page.expect_response(lambda r: r.status == 200 and _is_login_url(r.url),
                                      timeout=timeout_s * 1000) as info:
                page.goto(client.config.app_origin + "/", wait_until="domcontentloaded")
            resp = info.value
            data = resp.json()
            if "accountSession" not in data:
                raise Trading212Error(f"Unexpected login response from {resp.url}")
            headers = resp.request.all_headers()
            body = _request_json(resp.request)
            log.debug("Login captured via %s", resp.url.split("?")[0])
            page.wait_for_timeout(1500)          # the web app sets cookies via JS
            cookies = context.cookies()
        finally:
            try:
                context.close()
            except Exception as exc:     # driver already gone (e.g. after Ctrl-C)
                log.debug("Closing the browser failed: %s", exc)

    apply_browser_capture(client, data, headers, cookies, body)
    log.info("Browser login captured: account %s", client.account_id)
