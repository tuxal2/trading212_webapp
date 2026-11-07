"""Offline test: holding period of positions and the cached instrument catalog (no network).

    python tests/test_positions.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trading212.client as client_mod  # noqa: E402
from trading212 import Config, Trading212Client  # noqa: E402
from trading212.cli import _held  # noqa: E402
from trading212.history import Position  # noqa: E402

from test_offline import _resp  # noqa: E402

CATALOG = {"timestamp": 1, "instruments": [
    {"ticker": "INTC_US_EQ", "shortName": "INTC", "fullName": "Intel", "currency": "USD",
     "isin": "US4581401001", "type": "STOCK", "tradable": True},
    {"ticker": "ADSd_EQ", "shortName": "ADS", "fullName": "Adidas", "currency": "EUR",
     "isin": "DE000A1EWWW0", "type": "STOCK", "tradable": True},
    {"shortName": "broken entry without ticker"},
]}


def test_held_days():
    p = Position.from_json({"code": "INTC_US_EQ", "quantity": 1, "created": "2026-08-26T07:04:51.000+00:00"})
    assert p.first_buy.year == 2026 and p.first_buy.month == 8
    assert p.held_days(date(2026, 11, 7)) == 73
    assert Position.from_json({"code": "X_EQ", "quantity": 1}).held_days() is None
    assert Position.from_json({"code": "X_EQ", "quantity": 1, "created": "garbage"}).first_buy is None
    assert _held(73) == "73 d" and _held(400) == "1 y 35 d" and _held(None) == "-"


class Srv(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def request(self, method, url, **kw):
        assert url.endswith("/instrumentarium/v2/instruments/0")
        self.calls += 1
        return _resp(200, CATALOG, url)


def test_catalog_and_cache():
    with tempfile.TemporaryDirectory() as tmp:
        old = client_mod.CATALOG_CACHE
        client_mod.CATALOG_CACHE = Path(tmp) / "instruments.json"
        try:
            srv = Srv()
            t = Trading212Client(Config(session_file=None), session=srv)
            cat = t.instrument_catalog()
            assert set(cat) == {"INTC_US_EQ", "ADSd_EQ"}
            assert cat["ADSd_EQ"].full_name == "Adidas" and cat["ADSd_EQ"].currency == "EUR"
            assert cat["INTC_US_EQ"].isin == "US4581401001" and cat["INTC_US_EQ"].category == "EQUITY"
            t.instrument_catalog()
            assert srv.calls == 1                                   # in memory
            t2 = Trading212Client(Config(session_file=None), session=srv)
            assert t2.instrument_catalog()["INTC_US_EQ"].full_name == "Intel"
            assert srv.calls == 1                                   # from the disk cache
            old_time = time.time() - client_mod.CATALOG_MAX_AGE - 10
            os.utime(client_mod.CATALOG_CACHE, (old_time, old_time))
            Trading212Client(Config(session_file=None), session=srv).instrument_catalog()
            assert srv.calls == 2                                   # cache older than a day
        finally:
            client_mod.CATALOG_CACHE = old


if __name__ == "__main__":
    test_held_days()
    test_catalog_and_cache()
    print("OK")
