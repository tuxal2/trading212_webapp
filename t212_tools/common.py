"""Shared helpers of the t212 tools: options, login, output, confirmation."""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from decimal import Decimal
from pathlib import Path

from trading212 import Trading212Client
from trading212.config import load_profile_config
from trading212.exceptions import AuthenticationError


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-P", "--profile", help="trading212 profile (default: $T212_PROFILE)")
    p.add_argument("-c", "--config", help="config file (default: the profile's / shared config)")
    acc = p.add_mutually_exclusive_group()
    acc.add_argument("--demo", dest="account", action="store_const", const="demo",
                     help="use the demo account (practice money)")
    acc.add_argument("--live", dest="account", action="store_const", const="live",
                     help="use the live account (real money)")
    p.add_argument("--dry-run", action="store_true", help="only check and show, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="do it without asking")
    p.add_argument("-v", "--verbose", action="store_true")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")


def open_client(args) -> tuple[Trading212Client, Path | None]:
    """Client for the profile/config in ``args`` → (client, config file used)."""
    cfg, config_file = load_profile_config(args.profile, args.config)
    return Trading212Client(cfg), config_file


def login(t212: Trading212Client, args) -> bool:
    """Stored login (never asks); prints a hint and returns False if a login is needed."""
    try:
        t212.ensure_login(account=args.account)
        return True
    except AuthenticationError as exc:
        print(f"Not logged in ({exc.error_type or exc.code}) – run "
              f"'trading212{' --profile ' + args.profile if args.profile else ''} login' first.",
              file=sys.stderr)
        return False


def account_label(t212: Trading212Client) -> str:
    return "DEMO (practice money)" if t212.is_demo else "LIVE (real money)"


def load_section(config_file: Path | None, section: str) -> dict:
    """``[section]`` of the config file (empty if missing)."""
    if config_file and config_file.exists():
        return tomllib.loads(config_file.read_text(encoding="utf-8")).get(section, {})
    return {}


def err(exc: Exception) -> str:
    return (getattr(exc, "error_type", None) or getattr(exc, "code", None)
            or (exc.args[0] if exc.args else str(exc)))


def fmt(v: Decimal | None, cur: str = "", places: int = 2) -> str:
    if v is None:
        return "-"
    s = f"{v:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s + (f" {cur}" if cur else "")


def ask(prompt: str) -> str | None:
    """Ask on the terminal, also when the input came through a pipe; None = no terminal."""
    try:
        if sys.stdin.isatty():
            return input(prompt).strip().lower()
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip().lower()
    except (OSError, EOFError):
        return None


def confirm(args, question: str) -> int | None:
    """None = go ahead; otherwise the exit code to return (declined / no terminal)."""
    if args.yes:
        return None
    answer = ask(f"{question} [y/N] ")
    if answer is None:
        print("Nothing done: no terminal to confirm – use --yes.", file=sys.stderr)
        return 1
    if answer not in ("y", "yes", "j", "ja"):
        print("Nothing done.")
        return 0
    return None
