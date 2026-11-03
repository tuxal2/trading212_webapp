"""Instrument selection with the real search hits for "intel" (order as returned by Algolia)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading212 import ApiError, Config, Instrument, Trading212Client  # noqa: E402

HITS = json.loads((Path(__file__).parent / "intel_hits.json").read_text())


def client(**cfg) -> Trading212Client:
    t = Trading212Client(Config(session_file=None, **cfg))
    t.account_id = 1
    t.trading_type = "EQUITY"

    def fake_search(query, limit=10):
        q = query.upper()
        hits = [h for h in HITS if h["isin"] == q] if q.startswith("US") and len(q) == 12 else HITS
        return [Instrument.from_json(h) for h in hits]
    t.search_instruments = fake_search
    return t


def pick(sym, **kw):
    i = client().resolve_ticker(sym, **kw)
    return i.ticker, i.exchange, i.currency


def test_us_listing_default():
    for sym in ("INTC", "intc", "Intel", "intel", "US4581401001", "INTC_US_EQ"):
        assert pick(sym) == ("INTC_US_EQ", "NASDAQ", "USD"), (sym, pick(sym))


def test_never_cfd():
    assert all(not client().resolve_ticker(s).ticker == "INTC" for s in ("INTC", "Intel"))


def test_other_market():
    assert pick("Intel", market="Xetra") == ("INLd_EQ", "Deutsche Börse Xetra", "EUR")
    assert pick("US4581401001", market="Xetra") == ("INLd_EQ", "Deutsche Börse Xetra", "EUR")
    assert pick("INLd_EQ") == ("INLd_EQ", "Deutsche Börse Xetra", "EUR")   # exact ticker always wins
    assert pick("INL") == ("INLd_EQ", "Deutsche Börse Xetra", "EUR")


def test_exchange_aliases():
    for m in ("xetra", "Xetra", "XETRA", "de"):
        assert pick("Intel", market=m)[0] == "INLd_EQ", m
    for m in ("us", "nasdaq", "NASDAQ"):
        assert pick("Intel", market=m)[0] == "INTC_US_EQ", m


def test_exchange_not_available():
    try:
        pick("INTC", market="paris")
    except ApiError as e:
        assert "INTC_US_EQ" in e.body and "paris" in e.body
        return
    raise AssertionError("should have aborted")


def test_config_preference():
    t = client(preferred_market="Xetra", preferred_currency="EUR")
    assert t.resolve_ticker("Intel").ticker == "INLd_EQ"


def test_unknown():
    try:
        pick("XYZNOTHING123")
    except ApiError:
        return
    raise AssertionError


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(name, "OK")
