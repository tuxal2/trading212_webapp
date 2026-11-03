"""Configuration: config file (TOML) + environment variables + parameters.

Precedence (highest first):
  1. explicit parameters (e.g. CLI ``--username``)
  2. environment variables ``T212_USERNAME``, ``T212_PASSWORD``, ``T212_TOTP_SECRET``,
     ``T212_TURNSTILE_TOKEN``, ``T212_ACCOUNT``, ``T212_CONFIRM_ORDERS``, ``T212_CONFIG``
  3. config file (default: ``~/.config/trading212/config.toml``)
  4. defaults

Several Trading212 logins are kept apart with profiles: profile ``NAME`` uses the folder
``~/.config/trading212/NAME/`` (``config.toml``, ``session.json``, ``browser-profile/``).

The code contains NO credentials.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .exceptions import ConfigError

CONFIG_DIR = Path("~/.config/trading212").expanduser()
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"
DEFAULT_SESSION_PATH = CONFIG_DIR / "session.json"


def profile_dir(name: str) -> Path:
    """Folder of profile ``name``: ``~/.config/trading212/<name>/``."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name == "browser-profile":
        raise ConfigError(f"Invalid profile name {name!r} (letters, digits, . _ -)")
    return CONFIG_DIR / name


@dataclass
class Config:
    # --- [auth] ---
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    remember_me: bool = True
    # True asks Trading212 to trust this device; that may require an identity re-check and
    # can replace the device that is trusted so far (e.g. the phone)
    remember_device: bool = False
    # authenticator secret (base32 or otpauth:// URI): renews the login with a 2FA code
    totp_secret: str | None = field(default=None, repr=False)
    # Cloudflare Turnstile token (header X-Trader-TST), optional
    turnstile_token: str | None = field(default=None, repr=False)

    # --- [client] ---
    base_url: str = "https://live.services.trading212.com"
    app_origin: str = "https://app.trading212.com"
    app_version: str = "8.54.0"
    application: str = "WC4"
    device_model: str = "Chrome"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    )
    accept_language: str = "de-DE,de;q=0.8"
    target_currency: str = "EUR"
    timeout: float = 20.0
    session_file: Path | None = DEFAULT_SESSION_PATH
    # extra headers that are only sent with login requests
    extra_login_headers: dict[str, str] = field(default_factory=dict)

    account: str = "live"                # "live" or "demo" (practice money)

    # --- [orders] ---
    confirm_orders: bool = False         # True = CLI asks before sending an order
    preferred_market: str = "US"         # with several listings: "US" (NASDAQ/NYSE) or e.g. "Xetra", "" = any
    preferred_currency: str = "USD"      # preferred currency with several listings, "" = any
    # settlement currency (the order's currencyCode = wallet that pays / receives the proceeds):
    # "instrument" = trading currency of the listing (US stock → USD, Xetra → EUR, no conversion)
    # or a fixed one such as "EUR" (Trading212 then converts and charges an FX fee)
    settlement_currency: str = "instrument"

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, **overrides: Any) -> "Config":
        """Load config file, environment and ``overrides`` (``None`` values are ignored)."""
        cfg = cls()

        explicit = path is not None or "T212_CONFIG" in os.environ
        cfg_path = Path(path or os.environ.get("T212_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
        if cfg_path.exists():
            cfg._apply_file(cfg_path)
        elif explicit:
            raise ConfigError(f"Config file not found: {cfg_path}")

        env_map = {
            "T212_USERNAME": "username",
            "T212_PASSWORD": "password",
            "T212_TOTP_SECRET": "totp_secret",
            "T212_TURNSTILE_TOKEN": "turnstile_token",
        }
        if os.environ.get("T212_ACCOUNT"):
            cfg.account = os.environ["T212_ACCOUNT"].lower()
        if os.environ.get("T212_CONFIRM_ORDERS"):
            cfg.confirm_orders = os.environ["T212_CONFIRM_ORDERS"].lower() not in ("0", "false", "no", "off")
        for env, attr in env_map.items():
            if os.environ.get(env):
                setattr(cfg, attr, os.environ[env])

        for key, value in overrides.items():
            if value is None:
                continue
            if key not in {f.name for f in fields(cls)}:
                raise ConfigError(f"Unknown parameter: {key}")
            setattr(cfg, key, value)

        if isinstance(cfg.session_file, str):
            cfg.session_file = Path(cfg.session_file).expanduser() if cfg.session_file else None
        return cfg

    def _apply_file(self, path: Path) -> None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Config file {path} is not valid TOML: {exc}") from exc

        auth = data.get("auth", {})
        client = data.get("client", {})

        if (auth.get("password") or auth.get("totp_secret")) and sys.platform != "win32":
            mode = path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                warnings.warn(
                    f"{path} contains a password / 2FA secret and is readable by others. "
                    f"Recommended: chmod 600 {path}",
                    stacklevel=3,
                )

        for key in ("username", "password", "remember_me", "remember_device", "totp_secret",
                    "turnstile_token"):
            if key in auth and auth[key] != "":
                setattr(self, key, auth[key])

        if client.get("account"):
            self.account = str(client["account"]).lower()
        for key in ("base_url", "app_origin", "app_version", "application", "device_model",
                    "user_agent", "accept_language", "target_currency", "timeout"):
            if key in client and client[key] != "":
                setattr(self, key, client[key])

        if "session_file" in client:
            sf = client["session_file"]
            self.session_file = Path(sf).expanduser() if sf else None

        orders = data.get("orders", {})
        if "confirm" in orders:
            self.confirm_orders = bool(orders["confirm"])
        for key in ("preferred_market", "preferred_currency", "settlement_currency"):
            if key in orders:
                setattr(self, key, str(orders[key] or ""))

        if isinstance(client.get("extra_login_headers"), dict):
            self.extra_login_headers = {str(k): str(v) for k, v in client["extra_login_headers"].items()}
