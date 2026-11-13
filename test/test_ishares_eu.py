"""Tests for the iShares EU (UCITS) scraper and the shared parsers. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.parsers import parse_date, read_table, split_preamble, to_number
from etf_scraper.scrapers import ISharesEUListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")


def _read(name):
    return DATA_DIR.joinpath(name).read_text(encoding="utf-8")


# ------------------------------------------------------------------- parsers


@pytest.mark.parametrize(
    ["text", "expected"],
    [
        ("06/Nov/2026", date(2026, 11, 6)),
        ("14/Sept/2032", date(2032, 9, 14)),
        ("Nov 06, 2026", date(2026, 11, 6)),
        ("06.Nov.2026", date(2026, 11, 6)),
        ("2026-11-06", date(2026, 11, 6)),
        ("-", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_to_number():
    out = to_number(["1,234.56", "7.5%", "-", None, "abc"])
    assert out.iloc[0] == 1234.56
    assert out.iloc[1] == 7.5
    assert out.iloc[2:].isna().all()

    euro = to_number(["1.234,56", "0,5"], decimal=",", thousands=".")
    assert list(euro) == [1234.56, 0.5]


def test_split_preamble():
    text = 'Fund Holdings as of,"06/Nov/2026"\n\nTicker,Name,Weight (%)\n"A","Alpha","60"\n"-","Cash","-"\n'
    meta, table = split_preamble(text, ["Name", "Weight (%)"])
    assert meta == {"Fund Holdings as of": "06/Nov/2026"}
    df = read_table(table)
    assert list(df.columns) == ["Ticker", "Name", "Weight (%)"]
    assert len(df) == 2
    assert pd.isna(df["Ticker"].iloc[1]) and pd.isna(df["Weight (%)"].iloc[1])

    with pytest.raises(ValueError, match="No header line"):
        split_preamble(text, ["ISIN"])


# ------------------------------------------------------------------ holdings


def test_ishares_eu_equity_holdings_parser():
    """CSPX (iShares Core S&P 500 UCITS ETF) latest-holdings.csv"""
    holdings_df, as_of_date = ISharesEUListings._parse_holdings_resp(
        _read("ISharesEU_CSPX_holdings_06Nov2026.csv")
    )
    assert as_of_date == date(2026, 11, 6)
    assert len(holdings_df) == 510

    # calculated directly from the file
    assert holdings_df["amount"].sum() == 978609549.0
    assert np.isclose(holdings_df["market_value"].sum(), 153334347379.05)
    assert np.isclose(holdings_df["weight"].sum(), 100.02)
    assert holdings_df["ticker"].iloc[0] == "AAPL"
    assert holdings_df["ticker"].iloc[-1] == "ESZ6"  # futures position
    assert holdings_df["currency"].iloc[0] == "USD"


def test_ishares_eu_bond_holdings_parser():
    """iShares Global AAA-AA Govt Bond UCITS ETF, first 40 rows"""
    holdings_df, as_of_date = ISharesEUListings._parse_holdings_resp(
        _read("ISharesEU_AAAGOV_holdings_09Nov2026_first40.csv")
    )
    assert as_of_date == date(2026, 11, 9)
    assert len(holdings_df) == 40
    assert holdings_df["amount"].sum() == 68616000.0
    assert np.isclose(holdings_df["market_value"].sum(), 50064343.36)
    assert np.isclose(holdings_df["weight"].sum(), 21.43)

    last = holdings_df.iloc[-1]
    assert last["ticker"] == "DBR"
    assert last["coupon"] == 2.60
    assert last["maturity_date"] == pd.Timestamp("2034-08-15")
    assert holdings_df["effective_date"].notna().all()


def test_ishares_eu_normalized_output(monkeypatch):
    """Through ETFScraper the result uses the common holdings format"""
    text = _read("ISharesEU_CSPX_holdings_06Nov2026.csv")

    class _Resp:
        content = text.encode("utf-8")

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(ISharesEUListings.session, "get", fake_get)

    listings = pd.DataFrame([{
        "ticker": "CSPX",
        "isin": "IE00B5BMR087",
        "provider": "IShares_EU",
        "product_url": "https://www.ishares.com/uk/individual/en/products/253743/ishares-core-s-p-500-ucits-etf",
        "domicile": "IE",
    }])
    df = ETFScraper(listings).query_holdings("IE00B5BMR087")

    assert len(calls) == 1
    assert calls[0].startswith(
        "https://www.blackrock.com/varnish-api/uk-retail01-product-data/"
    )
    assert "portfolioId=253743&" in calls[0]
    assert "component=holdings" in calls[0]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_ticker"] == "CSPX").all()
    assert (df["fund_isin"] == "IE00B5BMR087").all()
    assert (df["provider"] == "IShares_EU").all()
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-11-06")


def test_ishares_eu_rejects_historical_dates():
    listing = SecurityListing(
        ticker="CSPX", provider="IShares_EU", product_url="https://example.test/p"
    )
    with pytest.raises(InvalidParameterError, match="latest holdings"):
        ISharesEUListings._retrieve_holdings(listing, date(2026, 1, 30))


# ------------------------------------------------------------------ listings


def test_ishares_eu_product_page_parser():
    info = ISharesEUListings._parse_product_page(
        _read("ISharesEU_CSPX_product_page_trimmed.html")
    )
    assert info == {
        "fund_name": "iShares Core S&P 500 UCITS ETF",
        "ticker": "CSPX",
        "asset_class": "Equity",
        "isin": "IE00B5BMR087",
    }


def test_ishares_eu_product_page_without_valid_isin():
    page = '<script type="application/ld+json">{"@graph":[{"@type":["InvestmentFund"],"name":"X","alternateName":"XX"}]}</script> (ISIN: IE00B5BMR088)'
    info = ISharesEUListings._parse_product_page(page)
    assert info["ticker"] == "XX"
    assert "isin" not in info  # bad check digit is ignored
