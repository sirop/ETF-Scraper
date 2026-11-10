"""Tests for the State Street (SPDR) EU UCITS scraper. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import SSGAEUListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")


def _read(name):
    return DATA_DIR.joinpath(name).read_bytes()


def test_ticker_slug_and_url():
    assert SSGAEUListings.ticker_slug("QUS5 GY") == "qus5-gy"
    assert SSGAEUListings.ticker_slug("SAWE IM^") == "sawe-im"
    assert SSGAEUListings.holdings_url("SPY5 GY").endswith(
        "/fund-data/etfs/emea/holdings-daily-emea-en-spy5-gy.xlsx"
    )


def test_ssga_eu_equity_holdings_parser():
    """QUS5 (SPDR S&P 500 Quality Aristocrats UCITS ETF)"""
    holdings_df, as_of_date, isin = SSGAEUListings._parse_holdings_resp(
        _read("SSGAEU_QUS5_holdings_20261109.xlsx")
    )
    assert as_of_date == date(2026, 11, 9)
    assert isin == "IE000FJJZA01"
    assert len(holdings_df) == 102  # disclaimer row after the table is dropped

    # calculated directly from the file with openpyxl
    assert holdings_df["amount"].sum() == 837164.0
    assert np.isclose(holdings_df["market_value"].sum(), 181485815.079)
    assert np.isclose(holdings_df["weight"].sum(), 100.000003)

    assert holdings_df["name"].iloc[0] == "Mastercard Incorporated Class A"
    assert holdings_df["isin"].iloc[0] == "US57636Q1040"
    # cash lines: "Unassigned" identifiers become missing
    assert holdings_df["name"].iloc[-1] == "U.S. Dollar"
    assert pd.isna(holdings_df["isin"].iloc[-1])
    assert pd.isna(holdings_df["sector"].iloc[-1])


def test_ssga_eu_bond_holdings_parser():
    """SGHY (State Street Global High Yield Bond Screened UCITS ETF)"""
    holdings_df, as_of_date, isin = SSGAEUListings._parse_holdings_resp(
        _read("SSGAEU_SGHY_holdings_20261109.xlsx")
    )
    assert as_of_date == date(2026, 11, 9)
    assert isin == "LU3357226557"
    assert len(holdings_df) == 2482

    assert np.isclose(holdings_df["amount"].sum(), 1036489694.180098)
    assert np.isclose(holdings_df["market_value"].sum(), 997628962.786798)
    assert np.isclose(holdings_df["weight"].sum(), 100.000015)

    assert pd.isna(holdings_df["isin"].iloc[0])  # cash line has "-"
    bond = holdings_df.iloc[1]
    assert bond["maturity_date"] == pd.Timestamp("2032-04-15")
    assert bond["coupon"] == 10.0
    assert holdings_df["isin"].iloc[-1] == "US15679GAC69"

    # Orsted 1000-year hybrid bond matures "14-Mar-3024": beyond the pandas 2 date
    # range, so missing on every pandas version rather than an error
    orsted = holdings_df[holdings_df["name"] == "OERSTED AS 5.125 03/14/3024"]
    assert len(orsted) == 1 and orsted["maturity_date"].isna().all()
    assert holdings_df["maturity_date"].notna().sum() > 2400


def test_ssga_eu_normalized_output_and_isin_check(monkeypatch):
    content = _read("SSGAEU_QUS5_holdings_20261109.xlsx")

    class _Resp:
        pass

    resp = _Resp()
    resp.content = content
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return resp

    monkeypatch.setattr(SSGAEUListings.session, "get", fake_get)

    listings = pd.DataFrame([{
        "ticker": "QUS5", "isin": "IE000FJJZA01", "product_id": "QUS5 GY",
        "provider": "SSGA_EU", "domicile": "IE",
    }])
    df = ETFScraper(listings).query_holdings("QUS5")
    assert calls[0].endswith("holdings-daily-emea-en-qus5-gy.xlsx")
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_isin"] == "IE000FJJZA01").all()
    assert (df["provider"] == "SSGA_EU").all()

    # a file for another fund than the listing says is rejected
    wrong = listings.assign(isin="IE00B6YX5C33")
    with pytest.raises(ValueError, match="holdings file is for IE000FJJZA01"):
        ETFScraper(wrong).query_holdings("QUS5")


def test_ssga_eu_rejects_historical_dates_and_missing_ticker():
    listing = SecurityListing(ticker="SPY5", provider="SSGA_EU", product_id="SPY5 GY")
    with pytest.raises(InvalidParameterError, match="latest holdings"):
        SSGAEUListings._retrieve_holdings(listing, date(2026, 1, 30))

    no_id = SecurityListing(ticker="SPY5", provider="SSGA_EU")
    with pytest.raises(InvalidParameterError, match="exchange ticker"):
        SSGAEUListings._retrieve_holdings(no_id)


def test_ssga_eu_fund_page_isin():
    page = "related IE00B6YX5C33 ... ISIN IE000FJJZA01 ... IE000FJJZA01 ... US0378331005 IE000FJJZA02"
    assert SSGAEUListings._parse_fund_page_isin(page) == "IE000FJJZA01"
    assert SSGAEUListings._parse_fund_page_isin("no isin here") is None
