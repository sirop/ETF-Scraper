"""Tests for the VanEck EU (UCITS) scraper. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import VanEckEUListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")
XLSX = DATA_DIR.joinpath("VanEckEU_SMH_holdings_20261110.xlsx")
PAGE = DATA_DIR.joinpath("VanEckEU_semiconductor-etf_page_20261111.html")
SMH_PAGE_URL = "https://www.vaneck.com/ie/en/investments/semiconductor-etf/"


def test_vaneck_holdings_parser():
    df = VanEckEUListings._parse_holdings_resp(XLSX.read_bytes())
    assert len(df) == 26
    first = df.iloc[0]
    assert first["name"] == "Advanced Micro Devices Inc"
    assert (first["ticker"], first["exchange"], first["isin"]) == ("AMD", "US", "US0079031078")
    assert first["amount"] == 1799609
    assert first["market_value"] == 915245145
    assert first["weight"] == 11.61
    assert np.isclose(df["weight"].sum(), 100.0)
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-11-10")
    assert df["file_fund_ticker"].iloc[0] == "SMH"
    cash = df.iloc[-1]
    assert cash["name"] == "Other/Cash" and pd.isna(cash["ticker"]) and pd.isna(cash["isin"])


@pytest.mark.parametrize(
    ["text", "expected"],
    [
        ("$ 915,245,145.00", 915245145.0),
        ("11.61%", 11.61),
        ("1,799,609", 1799609.0),
        ("-$ 1,234.50", -1234.5),
        ("$ -1,234.50", -1234.5),
        ("(1,234)", -1234.0),
        ("€ 12.00", 12.0),
        ("-", np.nan),
    ],
)
def test_vaneck_numbers(text, expected):
    result = VanEckEUListings._to_number([text]).iloc[0]
    assert (np.isnan(result) and np.isnan(expected)) or result == expected


def test_vaneck_rejects_html():
    with pytest.raises(InvalidParameterError, match="isn't an Excel file"):
        VanEckEUListings._parse_holdings_resp(b"<html>cookies disabled</html>")


def test_vaneck_sheet_name():
    assert VanEckEUListings._parse_sheet_name("SMH_asof_20261110") == (
        "SMH", pd.Timestamp("2026-11-10")
    )
    assert VanEckEUListings._parse_sheet_name("Sheet1") == (None, None)


def test_vaneck_query_holdings_normalized(monkeypatch):
    calls = []

    class _Resp:
        content = XLSX.read_bytes()

    def fake_get(url, expect=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(VanEckEUListings.session, "get", fake_get)
    listings = pd.DataFrame([{
        "ticker": "SMH", "isin": "IE00BMC38736", "provider": "VanEck_EU",
        "product_url": SMH_PAGE_URL, "product_id": "semiconductor-etf", "domicile": "IE",
    }])
    df = ETFScraper(listings).query_holdings("IE00BMC38736")
    assert calls == [SMH_PAGE_URL + "downloads/holdings/"]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_ticker"] == "SMH").all()
    assert (df["fund_isin"] == "IE00BMC38736").all()
    assert (df["provider"] == "VanEck_EU").all()


def test_vaneck_slug_from_product_url(monkeypatch):
    calls = []
    monkeypatch.setattr(
        VanEckEUListings, "retrieve_holdings", lambda slug, ticker="": calls.append(slug)
    )
    listing = SecurityListing(ticker="SMH", provider="VanEck_EU", product_url=SMH_PAGE_URL)
    VanEckEUListings._retrieve_holdings(listing)
    assert calls == ["semiconductor-etf"]


def test_vaneck_latest_only():
    listing = SecurityListing(ticker="SMH", provider="VanEck_EU", product_id="semiconductor-etf")
    with pytest.raises(InvalidParameterError, match="latest"):
        VanEckEUListings._retrieve_holdings(listing, date(2026, 10, 31))


def test_vaneck_fund_page_parser():
    info = VanEckEUListings.parse_fund_page(PAGE.read_text(encoding="utf-8"))
    assert info == {
        "fund_name": "VanEck Semiconductor UCITS ETF",
        "isin": "IE00BMC38736",
        "category": "Global",
        "ticker": "SMH",
    }
    assert VanEckEUListings.parse_fund_page("<html><title>Insights</title></html>") is None


def test_vaneck_slugs_from_saved_page():
    html = """
    <a href="https://www.vaneck.com/ie/en/investments/semiconductor-etf/">SMH</a>
    <a href="/ie/en/investments/gold-miners-etf/overview/">GDX</a>
    <a href="/ie/en/investments/semiconductor-etf/">again</a>
    <a href="/ie/en/insights/">not a fund</a>
    """
    assert VanEckEUListings.slugs_from_html(html) == ["semiconductor-etf", "gold-miners-etf"]


def test_vaneck_seed_file():
    seed = VanEckEUListings.load_seed()
    assert "semiconductor-etf" in set(seed["slug"])
    assert seed["slug"].is_unique


def test_vaneck_listings(monkeypatch):
    page = PAGE.read_text(encoding="utf-8")

    class _Resp:
        def __init__(self, text):
            self.text = text

    def fake_get(url, expect=None):
        if "semiconductor-etf" in url:
            return _Resp(page)
        return _Resp("<html><title>Page not found</title></html>")

    monkeypatch.setattr(VanEckEUListings.session, "get", fake_get)
    seed = pd.DataFrame({"slug": ["semiconductor-etf", "closed-etf"]})
    df = VanEckEUListings.retrieve_listings(seed)
    assert len(df) == 1
    row = df.iloc[0]
    assert (row["ticker"], row["isin"], row["product_id"]) == ("SMH", "IE00BMC38736", "semiconductor-etf")
    assert row["product_url"] == SMH_PAGE_URL
    assert (row["provider"], row["domicile"], row["fund_type"]) == ("VanEck_EU", "IE", "ETF")
