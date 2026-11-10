"""Tests for the Invesco EU (UCITS) scraper. No network access."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import InvescoEUListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")


def _load(name):
    return json.loads(DATA_DIR.joinpath(name).read_text(encoding="utf-8"))


def test_invesco_eu_holdings_url():
    assert InvescoEUListings.holdings_url("IE0032077012") == (
        "https://dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/"
        "IE0032077012/holdings/fund"
    )
    assert InvescoEUListings.holdings_url("IE0032077012", "index").endswith("/holdings/index")


def test_invesco_eu_equity_holdings_parser():
    """EQQQ (Invesco EQQQ Nasdaq-100 UCITS ETF Dist)"""
    holdings_df, as_of_date = InvescoEUListings._parse_holdings_resp(
        _load("InvescoEU_EQQQ_holdings_20261109_resp.json")
    )
    assert as_of_date == date(2026, 11, 9)
    assert len(holdings_df) == 102
    assert np.isclose(holdings_df["weight"].sum(), 100.01)
    assert holdings_df["name"].iloc[0] == "APPLE INC USD0.00001"
    assert holdings_df["isin"].iloc[0] == "US0378331005"
    assert holdings_df["isin"].iloc[-1] == "US02043Q1076"
    assert (holdings_df["currency"] == "USD").all()


def test_invesco_eu_bond_holdings_parser():
    """PEU (Invesco EUR bond ETF), last line is cash without ISIN"""
    holdings_df, as_of_date = InvescoEUListings._parse_holdings_resp(
        _load("InvescoEU_PEU_holdings_20261109_resp.json")
    )
    assert as_of_date == date(2026, 11, 9)
    assert len(holdings_df) == 55
    assert np.isclose(holdings_df["weight"].sum(), 99.99)
    assert holdings_df["isin"].iloc[0] == "DE000BU0E360"
    assert holdings_df["name"].iloc[-1] == "Cash and/or Derivatives"
    assert pd.isna(holdings_df["isin"].iloc[-1])


def test_invesco_eu_empty_response():
    """Newly launched funds return an empty object"""
    with pytest.raises(InvalidParameterError, match="no holdings published"):
        InvescoEUListings._parse_holdings_resp({})


def test_invesco_eu_normalized_output(monkeypatch):
    data = _load("InvescoEU_EQQQ_holdings_20261109_resp.json")

    class _Resp:
        def json(self):
            return data

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return _Resp()

    monkeypatch.setattr(InvescoEUListings.session, "get", fake_get)

    listings = pd.DataFrame([{
        "ticker": "EQQQ", "isin": "IE0032077012", "provider": "Invesco_EU", "domicile": "IE",
    }])
    df = ETFScraper(listings).query_holdings("EQQQ")

    assert calls == [(InvescoEUListings.holdings_url("IE0032077012"), {"idType": "isin"})]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_isin"] == "IE0032077012").all()
    assert (df["provider"] == "Invesco_EU").all()
    assert (df["holdings_variation"] == "fund").all()


def test_invesco_eu_rejects_bad_requests():
    listing = SecurityListing(ticker="EQQQ", provider="Invesco_EU", isin="IE0032077012")
    with pytest.raises(InvalidParameterError, match="latest holdings"):
        InvescoEUListings._retrieve_holdings(listing, date(2026, 1, 30))
    with pytest.raises(InvalidParameterError, match="No ISIN"):
        InvescoEUListings._retrieve_holdings(SecurityListing(ticker="X", provider="Invesco_EU"))
    with pytest.raises(ValueError, match="variation"):
        InvescoEUListings.retrieve_holdings("EQQQ", "IE0032077012", variation="top10")


def test_invesco_eu_listings_mapping(monkeypatch):
    products = pd.DataFrame([
        {"ticker": "EQQQ", "isin": "IE0032077012", "title": "Invesco EQQQ Nasdaq-100 UCITS ETF Dist",
         "bloombergTicker": "EQQQ LN", "assetType": "Equity", "replicationMethod": "Physical",
         "domicileCountry": "IE", "inceptionDate": "2002-12-02T00:00:00Z",
         "url": "/content/invesco/ie/en/financial-products/etfs/invesco-eqqq-nasdaq-100-ucits-etf-dist.html"},
        {"ticker": "XYZ", "isin": "IE00B3YCGJ38", "title": "Odd fund", "bloombergTicker": None,
         "assetType": "Equity", "replicationMethod": "Synthetic", "domicileCountry": "IE",
         "inceptionDate": None, "url": None},
    ])
    monkeypatch.setattr(InvescoEUListings, "_search_products", classmethod(lambda cls: products))
    lst = InvescoEUListings.retrieve_listings()

    eqqq = lst.iloc[0]
    assert eqqq["exchange"] == "London Stock Exchange"
    assert eqqq["product_id"] == "EQQQ LN"
    assert eqqq["inception_date"] == date(2002, 12, 2)
    assert eqqq["product_url"] == (
        "https://www.invesco.com/ie/en/financial-products/etfs/invesco-eqqq-nasdaq-100-ucits-etf-dist.html"
    )
    assert (lst["provider"] == "Invesco_EU").all()
    assert pd.isna(lst.iloc[1]["exchange"]) and pd.isna(lst.iloc[1]["product_id"])
