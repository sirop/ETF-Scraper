"""Tests for the Franklin Templeton UCITS ETF scraper. No network access."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import FranklinEUListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")


def _load(name):
    return json.loads(DATA_DIR.joinpath(name).read_text(encoding="utf-8"))


def _holdings():
    # FTSE Emerging Markets (fund 40163), trimmed to 34 of 1636 holdings
    return _load("FranklinEU_40163_Holdings_20261109.json")


def _identifiers():
    return _load("FranklinEU_IntlIndentifiers_20261111.json")


SITEMAP = """<urlset>
<url><loc>https://www.franklintempleton.de/produkte/preise-und-wertentwicklung-fonds/preise-etfs/produkt/40163/ETA/franklin-ftse-emerging-markets-ucits-etf/IE0004I037N4</loc></url>
<url><loc>https://www.franklintempleton.de/produkte/preise-und-wertentwicklung-fonds/preise-fonds/produkt/1234/A/some-mutual-fund/LU0000000001</loc></url>
<url><loc>https://www.franklintempleton.de/produkte/etf/aktive-etfs</loc></url>
</urlset>"""


def test_franklin_eu_holdings_parser():
    df = FranklinEUListings._parse_holdings_resp(_holdings()["data"])
    assert len(df) == 34
    assert (df["as_of_date"] == pd.Timestamp("2026-11-09")).all()
    # German number formats: "10.500,00" -> 10500.0, "-2.254,70" -> -2254.7
    assert np.isclose(df["weight"].sum(), 2.84)
    assert np.isclose(df["market_value"].sum(), 362916.4)
    first = df.iloc[0]
    assert (first["name"], first["ticker"], first["isin"], first["sedol"]) == (
        "FUBON FINANCIAL HO TWD 10", "2881", "TW0002881000", "6411673"
    )
    assert first["amount"] == 10500.0 and first["market_value"] == 48919.49
    assert pd.isna(first["cusip"])  # 7 character ids are SEDOLs
    assert pd.isna(first["sector"])  # "—" placeholder


def test_franklin_eu_holdings_special_rows():
    df = FranklinEUListings._parse_holdings_resp(_holdings()["data"]).set_index("name")
    pdd = df.loc["PDD HOLDI USD 0.00002 ADR"]
    assert pdd["cusip"] == "722304102" and pd.isna(pdd["sedol"])  # 9 characters: CUSIP
    cgt = df.loc["INR CGT ADJ"]  # tax adjustment: "INR CGT" is neither id
    assert pd.isna(cgt["cusip"]) and pd.isna(cgt["sedol"])
    assert cgt["market_value"] == -2254.70
    cash = df.loc["Net Current Assets"]
    assert cash["security_type"] == "CASH" and pd.isna(cash["amount"])
    assert df.loc["NEW TURKISH LIRA", "security_type"] == "CURRENCY"


def test_franklin_eu_no_holdings():
    with pytest.raises(InvalidParameterError, match="no holdings"):
        FranklinEUListings._parse_holdings_resp({"Portfolio": {"portfolio": {}}})


def test_franklin_eu_query_holdings_normalized(monkeypatch):
    sent = []

    def fake_graphql(operation, query, variables):
        sent.append((operation, variables))
        return _holdings()["data"]

    monkeypatch.setattr(FranklinEUListings, "_graphql", fake_graphql)
    listing = SecurityListing(
        ticker="EMGM", provider="Franklin_EU", isin="IE0004I037N4", product_id="40163/ETA"
    )
    df = FranklinEUListings.query_holdings(listing, None)
    assert sent == [("Holdings", {"fundid": "40163", "countrycode": "DE", "languagecode": "de_DE"})]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_isin"] == "IE0004I037N4").all()
    assert (df["provider"] == "Franklin_EU").all()


def test_franklin_eu_retrieve_errors():
    listing = SecurityListing(ticker="EMGM", provider="Franklin_EU", product_id="40163/ETA")
    with pytest.raises(InvalidParameterError, match="latest"):
        FranklinEUListings._retrieve_holdings(listing, date(2026, 10, 31))
    with pytest.raises(InvalidParameterError, match="fund id"):
        FranklinEUListings._retrieve_holdings(listing._replace(product_id=""), None)


def test_franklin_eu_graphql_body(monkeypatch):
    sent = {}

    class _Resp:
        def json(self):
            return {"errors": [{"message": "bad"}], "data": None}

    def fake_post(url, json=None, expect=None):
        sent.update(url=url, body=json, expect=expect)
        return _Resp()

    monkeypatch.setattr(FranklinEUListings.session, "post", fake_post)
    with pytest.raises(InvalidParameterError, match="bad"):
        FranklinEUListings.retrieve_holdings("EMGM", "40163")
    assert sent["url"] == "https://www.franklintempleton.de/api/pds/price-and-performance"
    assert sent["body"]["operationName"] == "Holdings"
    assert "dailyholdings" in sent["body"]["query"]
    assert sent["expect"] == ("json",)


def test_franklin_eu_listings_parser():
    df = FranklinEUListings.parse_listings_resp(_identifiers()["data"])
    assert len(df) == 62
    assert df["isin"].notna().sum() == 61  # one placeholder "-" dropped
    assert df["ticker"].notna().sum() == 53  # 52 tickers + 1 from the Bloomberg ticker
    row = df[df["isin"] == "IE0004I037N4"].iloc[0]
    assert (row["fund_id"], row["share_class"], row["ticker"]) == ("40163", "ETA", "EMGM")


@pytest.mark.parametrize(
    "fund, share_class, expected",
    [
        ("Franklin Future of Food UCITS ETF", "Franklin Future of Food UCITS ETF", "Franklin Future of Food UCITS ETF"),
        ("Franklin FTSE Emerging Markets UCITS ETF", "(Dis)", "Franklin FTSE Emerging Markets UCITS ETF (Dis)"),
        ("Franklin US Mega Cap 100 UCITS ETF", "EUR Hedged (Acc)", "Franklin US Mega Cap 100 UCITS ETF EUR Hedged (Acc)"),
        ("Franklin X UCITS ETF", None, "Franklin X UCITS ETF"),
        (None, "(Acc)", "(Acc)"),
    ],
)
def test_franklin_eu_share_class_name(fund, share_class, expected):
    assert FranklinEUListings._share_class_name(fund, share_class) == expected


def test_franklin_eu_listings(monkeypatch):
    data = _identifiers()["data"]
    for fund in data["PPSS"]:
        fund["fundname"], fund["assetclass"] = "Franklin Test UCITS ETF", "Aktien"
        for sc in fund["shareclass"]:
            sc["shclname"] = "(Acc)"
    monkeypatch.setattr(FranklinEUListings, "_graphql", lambda *a: data)

    class _Resp:
        text = SITEMAP

    monkeypatch.setattr(FranklinEUListings.session, "get", lambda url, expect=None, headers=None: _Resp())
    listings = FranklinEUListings.retrieve_listings()
    assert len(listings) == 61
    row = listings[listings["isin"] == "IE0004I037N4"].iloc[0]
    assert row["product_id"] == "40163/ETA"
    assert row["fund_name"] == "Franklin Test UCITS ETF (Acc)"
    assert row["asset_class"] == "Equity"  # translated from "Aktien"
    assert row["product_url"].endswith("/40163/ETA/franklin-ftse-emerging-markets-ucits-etf/IE0004I037N4")
    assert (listings["domicile"] == "IE").all()
    assert (listings["provider"] == "Franklin_EU").all()
    # only ETF pages (preise-etfs) are taken from the sitemap
    assert listings["product_url"].notna().sum() == 1
