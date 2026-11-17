"""Browser selection for the login (no browser is started)."""

import pytest

from trading212 import browser_login as bl
from trading212.exceptions import Trading212Error


def test_playwright_channels_pass_through():
    assert bl.resolve_browser(None) == (None, None)
    assert bl.resolve_browser("chrome") == ("chrome", None)
    assert bl.resolve_browser("MSEdge") == ("msedge", None)


def test_brave_is_found_on_path(monkeypatch):
    monkeypatch.delenv("T212_BROWSER_PATH", raising=False)
    monkeypatch.setattr(bl.shutil, "which", lambda n: "/usr/bin/brave-browser" if n == "brave-browser" else None)
    assert bl.resolve_browser("brave") == (None, "/usr/bin/brave-browser")


def test_brave_missing(monkeypatch):
    monkeypatch.delenv("T212_BROWSER_PATH", raising=False)
    monkeypatch.setattr(bl.shutil, "which", lambda n: None)
    monkeypatch.setattr(bl, "BROWSER_EXECUTABLES", {"brave": ["brave", "/nonexistent/brave"]})
    with pytest.raises(Trading212Error, match="--browser-path"):
        bl.resolve_browser("brave")


def test_explicit_path_wins(tmp_path, monkeypatch):
    exe = tmp_path / "mybrowser"
    exe.write_text("")
    assert bl.resolve_browser("chrome", str(exe)) == (None, str(exe))
    monkeypatch.setenv("T212_BROWSER_PATH", str(exe))
    assert bl.resolve_browser(None) == (None, str(exe))
    with pytest.raises(Trading212Error, match="not found"):
        bl.resolve_browser(None, str(tmp_path / "missing"))


def test_unknown_channel():
    with pytest.raises(Trading212Error, match="Unknown browser"):
        bl.resolve_browser("netscape")


class FakeChromium:
    def __init__(self):
        self.calls = []

    def launch_persistent_context(self, profile, **opts):
        self.calls.append(opts)
        return "ctx"


def test_launch_uses_executable_path(tmp_path):
    pw = type("PW", (), {"chromium": FakeChromium()})()
    assert bl._launch(pw, tmp_path / "prof", None, "/usr/bin/brave-browser") == "ctx"
    opts = pw.chromium.calls[0]
    assert opts["executable_path"] == "/usr/bin/brave-browser" and "channel" not in opts
    assert "--enable-automation" in opts["ignore_default_args"]
    assert "--disable-extensions" in opts["ignore_default_args"]          # extensions may load


def test_camoufox_channel_and_options(tmp_path):
    assert bl.resolve_browser("Camoufox") == ("camoufox", None)
    opts = bl.camoufox_options(tmp_path / "browser-profile")
    assert opts["headless"] is False and opts["humanize"] is True and opts["persistent_context"] is True
    assert opts["user_data_dir"].endswith("browser-profile-camoufox")          # not the Chromium profile
    assert "persistent_context" not in bl.camoufox_options(None)


def test_camoufox_login_retries_without_geoip(tmp_path, monkeypatch):
    import sys
    import types
    started = []

    class FakeCamoufox:
        def __init__(self, **opts):
            self.opts = opts

        def __enter__(self):
            started.append(self.opts["geoip"])
            if self.opts["geoip"]:
                raise RuntimeError("geoip extra missing")
            return "ctx"

        def __exit__(self, *a):
            started.append("closed")

    mod = types.ModuleType("camoufox.sync_api")
    mod.Camoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", mod)
    monkeypatch.setattr(bl, "_capture", lambda *a, **k: ({"accountSession": 1}, {}, [], None))
    assert bl._login_camoufox(object(), 5, tmp_path / "p")[0] == {"accountSession": 1}
    assert started == [True, False, "closed"]


def test_camoufox_missing_gives_install_hint(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", None)
    with pytest.raises(Trading212Error, match="camoufox fetch"):
        bl._login_camoufox(object(), 5, None)


def test_browser_options_before_and_after_login():
    from trading212.cli import _parser
    a = _parser().parse_args(["--profile", "stefan", "login", "--browser-channel", "camoufox", "--browser-timeout", "60"])
    assert (a.command, a.browser_channel, a.browser_timeout, a.profile) == ("login", "camoufox", 60, "stefan")
    b = _parser().parse_args(["--browser-channel", "brave", "login"])
    assert (b.browser_channel, b.browser_timeout, b.browser_path) == ("brave", 300, None)


class FakeField:
    def __init__(self, value=""):
        self.value = value

    @property
    def first(self):
        return self

    def wait_for(self, **kw):
        pass

    def input_value(self):
        return self.value

    def fill(self, v):
        self.value = v


class FakePage:
    def __init__(self, email="", pw=""):
        self.fields = {bl.EMAIL_FIELD: FakeField(email), bl.PASSWORD_FIELD: FakeField(pw)}
        self.clicked = False

    def locator(self, sel):
        return self.fields[sel]

    def click(self, *a, **k):                       # must never be called
        self.clicked = True


def test_prefill_fills_but_never_submits():
    page = FakePage()
    assert bl._prefill(page, "me@example.com", "secret")
    assert page.fields[bl.EMAIL_FIELD].value == "me@example.com"
    assert page.fields[bl.PASSWORD_FIELD].value == "secret"
    assert not page.clicked


def test_prefill_keeps_existing_values_and_needs_credentials():
    page = FakePage(email="from-password-manager@x")
    bl._prefill(page, "me@example.com", "secret")
    assert page.fields[bl.EMAIL_FIELD].value == "from-password-manager@x"
    assert not bl._prefill(FakePage(), None, None)


def test_password_file(tmp_path):
    from trading212.config import Config
    (tmp_path / "pw").write_text("s3cret\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[auth]\nusername = "me@example.com"\npassword_file = "pw"\n')
    cfg.chmod(0o600)
    c = Config.load(cfg)
    assert (c.username, c.password) == ("me@example.com", "s3cret")


# ------------------------------------------------------------------ automatic submit + 2FA

class El:
    def __init__(self, page, visible=True, maxlength=None):
        self.page, self.visible, self.maxlength, self.value, self.pressed = page, visible, maxlength, "", []

    def is_visible(self):
        return self.visible

    def fill(self, v):
        self.value = v

    def press(self, key):
        self.pressed.append(key)
        self.page.events.append(("enter", self))
        self.page.on_enter(self)

    def get_attribute(self, name):
        return self.maxlength if name == "maxlength" else None


class Loc:
    def __init__(self, els):
        self.els = els

    def count(self):
        return len(self.els)

    @property
    def first(self):
        return self.els[0]

    def all(self):
        return list(self.els)


class AutoPage:
    """Login form → after Enter a 2FA form appears (one field or six boxes)."""

    def __init__(self, boxes=0, turnstile=False):
        self.events, self.boxes, self.otp = [], boxes, []
        self.password = El(self)
        self.turnstile = turnstile

    def locator(self, sel):
        if sel == bl.PASSWORD_FIELD:
            return Loc([self.password])
        if sel == bl.OTP_FIELD:
            return Loc(self.otp)
        if sel == bl.TURNSTILE_FIELD:
            return Loc([El(self)] if self.turnstile else [])
        raise KeyError(sel)

    def get_by_role(self, role, name=None):
        return Loc([])                       # no named button → Enter is used

    def on_enter(self, el):
        if el is self.password:              # submitted → 2FA form (Trading212: six boxes, no maxlength)
            self.otp = [El(self) for _ in range(self.boxes)] if self.boxes else [El(self)]

    def wait_for_timeout(self, ms):
        pass

    def wait_for_function(self, js, timeout=None):
        self.events.append(("turnstile-wait", None))


def test_auto_login_submits_and_enters_totp(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(bl, "totp", lambda secret: "123456")
    monkeypatch.setattr(bl.time, "time", lambda: 1000.0)              # not near the 30 s boundary
    page = AutoPage()
    bl._auto_login(page, SimpleNamespace(totp_secret="S"), {"v": False})
    assert page.password.pressed == ["Enter"]
    assert page.otp[0].value == "123456" and page.otp[0].pressed == ["Enter"]


def test_auto_login_fills_digit_boxes_and_waits_for_turnstile(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(bl, "totp", lambda secret: "654321")
    monkeypatch.setattr(bl.time, "time", lambda: 1000.0)
    page = AutoPage(boxes=6, turnstile=True)
    bl._auto_login(page, SimpleNamespace(totp_secret="S"), {"v": False})
    assert page.events[0] == ("turnstile-wait", None)
    assert "".join(b.value for b in page.otp) == "654321"


def test_auto_login_without_totp_secret_stops_after_submit():
    from types import SimpleNamespace
    page = AutoPage()
    bl._auto_login(page, SimpleNamespace(totp_secret=None), {"v": False})
    assert page.password.pressed == ["Enter"] and page.otp[0].value == ""


def test_login_options_parse():
    from trading212.cli import _parser
    a = _parser().parse_args(["login", "--no-submit"])
    assert a.no_submit and not a.no_prefill


def test_debugger_writes_structure_and_screenshot(tmp_path):
    shots = []

    class P:
        def evaluate(self, js):
            assert "filled" in js and ".value)" not in js.replace("e.value && e.value.length", "")  # no values
            return {"url": "https://app.trading212.com/", "inputs": [{"type": "password", "filled": True}]}

        def screenshot(self, path, full_page):
            shots.append(path)
            open(path, "wb").write(b"png")

    d = bl.Debugger(tmp_path / "dbg")
    d.dump(P(), "After submit")
    d.dump(P(), "2fa form")
    files = sorted(p.name for p in (tmp_path / "dbg").iterdir())
    assert files == ["01-after-submit.json", "01-after-submit.png", "02-2fa-form.json", "02-2fa-form.png"]
    import json
    assert json.loads((tmp_path / "dbg" / "01-after-submit.json").read_text())["step"] == "After submit"
    bl.NO_DEBUG.dump(P(), "x")                                  # no directory → nothing happens


def test_trading212_2fa_form_names():
    assert bl.CONFIRM_NAME.match("Fortfahren") and bl.CONFIRM_NAME.match(" Continue ")
    assert bl.OTP_FIELD.startswith('input[data-testid="2fa-digit-input"]')


def test_headless_values_and_requirements(tmp_path, monkeypatch):
    from types import SimpleNamespace
    assert bl.parse_headless(None) is False and bl.parse_headless("off") is False
    assert bl.parse_headless("on") is True and bl.parse_headless("virtual") == "virtual"
    with pytest.raises(Trading212Error):
        bl.parse_headless("maybe")
    assert bl.camoufox_options(None, headless="virtual")["headless"] == "virtual"
    monkeypatch.delenv("T212_HEADLESS", raising=False)
    no_totp = SimpleNamespace(config=SimpleNamespace(username="u", password="p", totp_secret=None))
    with pytest.raises(Trading212Error, match="totp_secret"):
        bl.browser_login(no_totp, channel="camoufox", headless="on")
    with pytest.raises(Trading212Error, match="camoufox"):
        bl.browser_login(no_totp, channel="chrome", headless="virtual")


def test_headless_is_passed_to_the_browsers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(bl, "_login_camoufox", lambda *a: seen.update(cf=a[-1]) or ({}, {}, [], None))
    monkeypatch.setattr(bl, "_login_chromium", lambda *a: seen.update(ch=a[-1]) or ({}, {}, [], None))
    monkeypatch.setattr(bl, "apply_browser_capture", lambda *a: None)
    client = SimpleNamespace(config=SimpleNamespace(username="u", password="p", totp_secret="S"), account_id=1)
    bl.browser_login(client, channel="camoufox", headless="virtual")
    bl.browser_login(client, channel="chrome", headless="on")
    assert seen == {"cf": "virtual", "ch": True}
    pw = type("PW", (), {"chromium": FakeChromium()})()
    bl._launch(pw, tmp_path / "prof", None, "/usr/bin/brave-browser", headless=True)
    assert pw.chromium.calls[0]["headless"] is True
