"""Tests for the Amundi scraper. No network access."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import AmundiListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")
RESP_FILE = "Amundi_LU1681038839_getProductsData_20261106.json"


def _resp():
    return json.loads(DATA_DIR.joinpath(RESP_FILE).read_text(encoding="utf-8"))


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.amundietf.de/de/privatanleger/products/equity/amundi-russell-2000-ucits-etf-usd-acc/lu1681038839</loc></url>
<url><loc>https://www.amundietf.de/de/professionell/products/equity/amundi-russell-2000-ucits-etf-usd-acc/lu1681038839</loc></url>
<url><loc>https://www.amundietf.de/de/professionell/products/fixed-income/amundi-euro-high-yield-bond-esg-ucits-etf-dist/lu1215415214</loc></url>
<url><loc>https://www.amundietf.de/de/privatanleger/products/equity/amundi-russell-2000-ucits-etf-usd-acc/lu1681038839</loc></url>
<url><loc>https://www.amundietf.de/de/privatanleger/products/equity/broken/lu1681038830</loc></url>
<url><loc>https://www.amundietf.de/de/privatanleger/rechtshinweise</loc></url>
</urlset>"""


def test_amundi_holdings_parser():
    """LU1681038839 (Russell 2000, swap based): substitute basket plus a cash line"""
    df = AmundiListings._parse_holdings_resp(_resp()["products"][0])
    assert len(df) == 66
    assert (df["as_of_date"] == pd.Timestamp("2026-11-06")).all()
    assert np.isclose(df["weight"].sum(), 98.54218988708693)  # percent, not fractions
    assert np.isclose(df["amount"].sum(), 12707772.0)
    first = df.iloc[0]
    assert (first["name"], first["isin"], first["ticker"], first["exchange"]) == (
        "ASML HOLDING NV", "NL0010273215", "ASML", "NA"
    )
    assert first["bloomberg_ticker"] == "ASML NA"
    assert first["security_type"] == "EQUITY_ORDINARY"
    assert first["location"] == "Netherlands"
    cash = df[df["security_type"] == "CASH"]
    assert len(cash) == 1 and pd.isna(cash["ticker"].iloc[0])


def test_amundi_no_holdings():
    with pytest.raises(InvalidParameterError, match="no holdings"):
        AmundiListings._parse_holdings_resp({"productId": "X", "composition": None})


def test_amundi_query_holdings_normalized(monkeypatch):
    calls = []

    def fake_query(isins, characteristics, composition=False):
        calls.append((isins, composition))
        return _resp()["products"]

    monkeypatch.setattr(AmundiListings, "_query_products", fake_query)
    listing = SecurityListing(
        ticker="RS2K", provider="Amundi", isin="LU1681038839"
    )
    df = AmundiListings.query_holdings(listing, None)
    assert calls == [(["LU1681038839"], True)]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_isin"] == "LU1681038839").all()
    assert (df["provider"] == "Amundi").all()


def test_amundi_latest_only():
    listing = SecurityListing(ticker="RS2K", provider="Amundi", isin="LU1681038839")
    with pytest.raises(InvalidParameterError, match="latest"):
        AmundiListings._retrieve_holdings(listing, date(2026, 10, 31))


def test_amundi_unknown_isin(monkeypatch):
    monkeypatch.setattr(AmundiListings, "_query_products", lambda *a, **k: [])
    with pytest.raises(InvalidParameterError, match="unknown ISIN"):
        AmundiListings.retrieve_holdings("X", "LU0000000000")


def test_amundi_request_body(monkeypatch):
    """The request carries the public retail page context and the composition fields"""
    sent = {}

    class _Resp:
        def json(self):
            return {"products": []}

    def fake_post(url, json=None, expect=None):
        sent.update(url=url, body=json, expect=expect)
        return _Resp()

    monkeypatch.setattr(AmundiListings.session, "post", fake_post)
    AmundiListings._query_products(["LU1681038839"], ["ISIN"], composition=True)
    assert sent["url"] == "https://www.amundietf.de/mapi/ProductAPI/getProductsData"
    assert sent["body"]["context"] == {
        "countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"
    }
    assert sent["body"]["productIds"] == ["LU1681038839"]
    assert "weight" in sent["body"]["composition"]["compositionFields"]
    assert sent["expect"] == ("json",)


def test_amundi_sitemap_parser():
    pages = AmundiListings.parse_sitemap(SITEMAP)
    # both sections, de-duplicated by ISIN, invalid ISIN check digit dropped,
    # stored as the retail page URL
    assert pages["isin"].tolist() == ["LU1681038839", "LU1215415214"]
    assert pages["asset_class"].tolist() == ["equity", "fixed-income"]
    assert pages["product_url"].str.contains("/privatanleger/").all()


def test_amundi_listings(monkeypatch):
    class _Resp:
        text = SITEMAP

    sent = {}

    def fake_get(url, expect=None, headers=None):
        sent["accept"] = (headers or {}).get("Accept", "")
        return _Resp()

    monkeypatch.setattr(AmundiListings.session, "get", fake_get)
    products = [
        {
            "productId": "LU1681038839",
            "characteristics": {
                "ISIN": "LU1681038839",
                "SHARE_MARKETING_NAME": "Amundi Russell 2000 UCITS ETF USD Acc",
                "TICKER_BLOOMBERG": "RS2K FP",
            },
        },
        # no characteristics: name falls back to the URL slug
        {"productId": "LU1215415214", "characteristics": {}},
    ]
    monkeypatch.setattr(AmundiListings, "_query_products", lambda *a, **k: products)
    listings = AmundiListings.retrieve_listings()
    assert sent["accept"].startswith("application/xml")  # json gets a 404
    assert len(listings) == 2
    first, second = listings.iloc[0], listings.iloc[1]
    assert (first["ticker"], first["product_id"], first["domicile"]) == ("RS2K", "RS2K FP", "LU")
    assert first["fund_name"] == "Amundi Russell 2000 UCITS ETF USD Acc"
    assert first["asset_class"] == "Equity"
    assert second["fund_name"] == "Amundi Euro High Yield Bond Esg Ucits Etf Dist"
    assert pd.isna(second["ticker"])
    assert (listings["provider"] == "Amundi").all()
    assert (listings["fund_type"] == "ETF").all()
