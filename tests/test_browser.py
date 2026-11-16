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
