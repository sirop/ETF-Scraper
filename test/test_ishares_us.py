"""Tests for the iShares US scraper with the latest-holdings.csv download. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import ISharesListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")
IVV_URL = "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf"


def _read(name):
    return DATA_DIR.joinpath(name).read_text(encoding="utf-8-sig")


def test_ishares_us_equity_layout():
    """Equity funds: Type column, Market Weight + Notional Weight instead of Weight (%)"""
    df, as_of = ISharesListings._parse_holdings_resp(
        _read("IShares_IVV_latest_holdings_20261109.csv")
    )
    assert as_of == date(2026, 11, 9)
    assert len(df) == 509
    assert df["ticker"].iloc[0] == "AAPL"
    assert df["weight"].iloc[0] == 7.86
    assert df["security_type"].iloc[0] == "EQUITY"
    assert np.isclose(df["weight"].sum(), 99.99)
    assert df["amount"].sum() == 5402462995.0
    assert "notional_weight" in df

    # holdings without a weight ("-") are kept: futures, collateral, unlisted stocks
    no_weight = df[df["weight"].isna()]
    assert set(no_weight["ticker"]) == {"ESZ6", "SGAFT", "HOLX", "SKYD WS"}
    assert df.loc[df["ticker"] == "ESZ6", "notional_weight"].iloc[0] == 0.06
    assert df.loc[df["ticker"] == "HOLX", "exchange"].iloc[0] == "NO MARKET (E.G. UNLISTED)"
    # placeholders are missing values, not "-"
    assert df.loc[df["ticker"] == "SGAFT", "exchange"].isna().all()


def test_ishares_us_bond_layout():
    df, as_of = ISharesListings._parse_holdings_resp(
        _read("IShares_AGG_latest_holdings_20261109_trimmed.csv"), "Fixed Income"
    )
    assert as_of == date(2026, 11, 9)
    assert len(df) == 40
    assert df["cusip"].iloc[0] == "066922519"  # leading zero kept
    assert df["isin"].iloc[1] == "US91282CMM00"
    assert df["maturity"].iloc[1] == "Feb 15, 2035"
    assert df["effective_date"].iloc[1] == pd.Timestamp("2025-02-18")
    assert pd.isna(df["accrual_date"].iloc[0])
    assert np.isclose(df["weight"].sum(), 15.51)


def test_ishares_us_commodity_layout():
    """Previously raised NotImplementedError for this asset class"""
    df, _ = ISharesListings._parse_holdings_resp(
        _read("IShares_GSG_latest_holdings_20261109.csv"), "Commodities & Digital Assets"
    )
    assert len(df) == 26
    assert df["security_type"].iloc[0] == "FUTURE"
    assert np.isclose(df["weight"].sum(), 100.03)


def test_ishares_us_mutual_fund_layout():
    """Short preamble without the fund name, four columns only"""
    df, as_of = ISharesListings._parse_holdings_resp(
        _read("IShares_BIRDX_latest_holdings_20260930.csv"), "Real Estate"
    )
    assert as_of == date(2026, 9, 30)
    assert list(df.columns) == ["name", "market_value", "weight", "amount"]
    assert len(df) == 368
    assert df["name"].iloc[0] == "WELLTOWER"
    assert np.isclose(df["weight"].sum(), 100.02)


def test_ishares_us_rejects_html():
    with pytest.raises(ValueError):
        ISharesListings._parse_holdings_resp("<html><body>Not a CSV</body></html>")


def _fake_get(text, calls):
    class _Resp:
        content = text.encode("utf-8")

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp()

    return fake_get


def _ivv_listing():
    return pd.DataFrame([{
        "ticker": "IVV",
        "isin": "US4642872000",
        "provider": "IShares",
        "asset_class": "Equity",
        "product_url": IVV_URL,
    }])


def test_ishares_us_normalized_output(monkeypatch):
    calls = []
    text = _read("IShares_IVV_latest_holdings_20261109.csv")
    monkeypatch.setattr(ISharesListings.session, "get", _fake_get(text, calls))

    df = ETFScraper(_ivv_listing()).query_holdings("IVV")

    assert calls == [IVV_URL + "/latest-holdings.csv"]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_ticker"] == "IVV").all()
    assert (df["provider"] == "IShares").all()
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-11-09")


def test_ishares_us_dates(monkeypatch):
    """A date works only if it is the date of the latest file"""
    calls = []
    text = _read("IShares_IVV_latest_holdings_20261109.csv")
    monkeypatch.setattr(ISharesListings.session, "get", _fake_get(text, calls))
    scraper = ETFScraper(_ivv_listing())

    df = scraper.query_holdings("IVV", date(2026, 11, 9))
    assert len(df) == 509

    with pytest.raises(InvalidParameterError, match="N-PORT"):
        scraper.query_holdings("IVV", date(2026, 10, 30))


def test_ishares_us_closed_fund(monkeypatch):
    def fake_get(url, **kwargs):
        resp = requests.Response()
        resp.status_code = 400
        raise requests.HTTPError("400", response=resp)

    monkeypatch.setattr(ISharesListings.session, "get", fake_get)
    with pytest.raises(InvalidParameterError, match="HTTP 400"):
        ISharesListings.retrieve_holdings("EAOA", IVV_URL)
