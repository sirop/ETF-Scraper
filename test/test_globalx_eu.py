"""Tests for the Global X Europe scraper. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import GlobalXEUListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")


def _read(name):
    return DATA_DIR.joinpath(name).read_text(encoding="utf-8-sig")


DISCLAIMER = "The information contained herein may not be reproduced, redistributed or used to create any derivative works.\n"


def test_globalx_eu_holdings_parser_physical():
    """BOTZ topholdingscsv: all holdings of a physically replicated fund"""
    df = GlobalXEUListings._parse_holdings_resp(_read("GlobalXEU_BOTZ_topholdingscsv_20261109.csv"))
    assert len(df) == 67  # disclaimer line dropped
    assert (df["as_of_date"] == pd.Timestamp("2026-11-09")).all()
    assert np.isclose(df["weight"].sum(), 99.997)
    assert np.isclose(df["amount"].sum(), 14631679.3)
    assert np.isclose(df["market_value"].sum(), 148940642.05)
    first = df.iloc[0]
    assert (first["name"], first["isin"], first["sedol"], first["location"]) == (
        "KEYENCE CORP", "JP3236200006", "6490995", "JP"
    )
    assert df["cusip"].isna().all()


def test_globalx_eu_holdings_parser_basket():
    """QYLD basketconstituentscsv: 9 character ids in the SEDOL column are CUSIPs"""
    df = GlobalXEUListings._parse_holdings_resp(_read("GlobalXEU_QYLD_basketconstituentscsv_20261109.csv"))
    assert len(df) == 44
    assert np.isclose(df["weight"].sum(), 99.244)
    assert np.isclose(df["market_value"].sum(), 915692043.74)
    assert df["cusip"].iloc[0] == "478160104"  # Johnson & Johnson
    assert pd.isna(df["sedol"].iloc[0])
    assert df["name"].iloc[-1] == "EURO"


def test_globalx_eu_empty_files():
    assert GlobalXEUListings._parse_holdings_resp("").empty
    assert GlobalXEUListings._parse_holdings_resp(DISCLAIMER).empty
    with pytest.raises(ValueError, match="Unexpected holdings CSV header"):
        GlobalXEUListings._parse_holdings_resp("FOO,BAR\n1,2\n")


class _Resp:
    def __init__(self, text):
        self.content = text.encode("utf-8")


def test_globalx_eu_falls_back_to_basket(monkeypatch):
    basket = _read("GlobalXEU_QYLD_basketconstituentscsv_20261109.csv")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        return _Resp(DISCLAIMER if url.endswith("topholdingscsv") else basket)

    monkeypatch.setattr(GlobalXEUListings.session, "get", fake_get)
    listings = pd.DataFrame([{
        "ticker": "QYLD", "isin": "IE00BM8R0J59", "product_id": "qyld",
        "provider": "GlobalX_EU", "domicile": "IE",
    }])
    df = ETFScraper(listings).query_holdings("IE00BM8R0J59")

    assert calls == ["topholdingscsv", "basketconstituentscsv"]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["holdings_source"] == "basketconstituentscsv").all()
    assert (df["fund_isin"] == "IE00BM8R0J59").all()


def test_globalx_eu_no_holdings(monkeypatch):
    monkeypatch.setattr(GlobalXEUListings.session, "get", lambda url, **kw: _Resp(DISCLAIMER))
    with pytest.raises(InvalidParameterError, match="no holdings published"):
        GlobalXEUListings.retrieve_holdings("AVMX", "avmx")


def test_globalx_eu_rejects_bad_requests():
    listing = SecurityListing(ticker="BOTZ", provider="GlobalX_EU", product_id="botz")
    with pytest.raises(InvalidParameterError, match="latest holdings"):
        GlobalXEUListings._retrieve_holdings(listing, date(2026, 1, 30))
    with pytest.raises(InvalidParameterError, match="product_id"):
        GlobalXEUListings._retrieve_holdings(SecurityListing(ticker="BOTZ", provider="GlobalX_EU"))


def test_globalx_eu_listings_share_classes(monkeypatch):
    """One row per share class; income funds (distributing primary class) get one row"""
    botz_page = _read("GlobalXEU_BOTZ_page_trimmed.html")
    income_page = (
        "<title>Nasdaq 100 Covered Call UCITS ETF (QYLD) | Global X ETFs Europe</title>"
        '<div>Primary ISIN</div><div class="x">IE00BM8R0J59</div>'
        '"distIsin":"IE00BM8R0J59","dist":[{"ticker":"QYLD LN"}]'
    )
    pages = {"botz": botz_page, "qyld": income_page}
    monkeypatch.setattr(GlobalXEUListings, "list_fund_slugs", classmethod(lambda cls: ["botz", "qyld"]))
    monkeypatch.setattr(
        GlobalXEUListings.session, "get",
        lambda url, **kw: type("R", (), {"text": pages[url.rsplit("/", 1)[-1]]})(),
    )
    lst = GlobalXEUListings.retrieve_listings()
    assert list(zip(lst["ticker"], lst["isin"])) == [
        ("BOTZ", "IE00BLCHJB90"), ("BOTG", "IE00BLCHJC08"), ("QYLD", "IE00BM8R0J59"),
    ]
    assert (lst["product_id"] == ["botz", "botz", "qyld"]).all()
    assert (lst["fund_type"] == "ETF").all()


def test_globalx_eu_fund_page_parser():
    info = GlobalXEUListings._parse_fund_page(_read("GlobalXEU_BOTZ_page_trimmed.html"))
    assert info["fund_name"] == "Robotics & Artificial Intelligence UCITS ETF (BOTZ)"
    assert info["isin"] == "IE00BLCHJB90"
    assert info["dist_isin"] == "IE00BLCHJC08"
    assert info["dist_ticker"] == "BOTG"
