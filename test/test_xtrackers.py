"""Tests for the Xtrackers (DWS) scraper. No network access."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import XtrackersListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")
# MSCI World 1C, holdings as of 09.11.2026: first 25 rows, 5 cash/other rows, last 5 rows
HOLDINGS = DATA_DIR.joinpath("Xtrackers_IE00BJ0KDQ92_holdings_20261111_trimmed.json")


def _holdings_json():
    return json.loads(HOLDINGS.read_text(encoding="utf-8"))


def test_xtrackers_holdings_parser():
    df = XtrackersListings._parse_holdings_resp(_holdings_json())
    assert len(df) == 35
    first = df.iloc[0]
    assert (first["isin"], first["name"]) == ("US0378331005", "APPLE")
    assert np.isclose(first["weight"], 5.75024052)  # exact value, not the rounded "5,750%"
    assert np.isclose(first["market_value"], 1775472981.31)
    assert (first["location"], first["sector"], first["security_type"]) == (
        "Vereinigte Staaten von Amerika", "Informationstechnologie", "Aktien"
    )
    assert first["currency"] == "USD"
    assert (df["as_of_date"] == pd.Timestamp("2026-11-09")).all()


def test_xtrackers_cash_and_placeholders():
    df = XtrackersListings._parse_holdings_resp(_holdings_json())
    cash = df[df["security_type"] == "Cash"]
    assert len(cash) >= 3
    assert cash["isin"].isna().all()  # "_CURRENCYUSD" etc are not ISINs
    assert {"USD", "EUR", "DKK"} <= set(cash["currency"])
    eur = cash[cash["name"] == "EURO CURRENCY"].iloc[0]
    assert pd.isna(eur["location"])  # "--"
    assert pd.isna(eur["sector"])  # "unbekannt"


def test_xtrackers_rejects_unexpected_tables():
    with pytest.raises(InvalidParameterError, match="no holdings"):
        XtrackersListings._parse_holdings_resp({"tables": []})
    data = _holdings_json()
    data["tables"][0]["columns"] = [{"key": "header", "value": "Something else"}]
    with pytest.raises(InvalidParameterError, match="unexpected holdings columns"):
        XtrackersListings._parse_holdings_resp(data)


def test_xtrackers_query_holdings_normalized(monkeypatch):
    calls = []

    class _Resp:
        def json(self):
            return _holdings_json()

    def fake_get(url, expect=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(XtrackersListings.session, "get", fake_get)
    listings = pd.DataFrame([{
        "ticker": pd.NA, "isin": "IE00BJ0KDQ92", "provider": "Xtrackers", "domicile": "IE",
        "product_url": "https://etf.deutsche-am.com/de-de/IE00BJ0KDQ92-msci-world-ucits-etf-1c/",
    }])
    df = ETFScraper(listings).query_holdings("IE00BJ0KDQ92")
    assert calls == ["https://etf.deutsche-am.com/api/pdp/de-de/etf/IE00BJ0KDQ92/holdings"]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_isin"] == "IE00BJ0KDQ92").all()
    assert (df["provider"] == "Xtrackers").all()
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-11-09")


def test_xtrackers_latest_only():
    listing = SecurityListing(ticker="", provider="Xtrackers", isin="IE00BJ0KDQ92")
    with pytest.raises(InvalidParameterError, match="latest"):
        XtrackersListings._retrieve_holdings(listing, date(2026, 10, 31))


@pytest.mark.parametrize(
    ["slug", "expected"],
    [
        ("msci-world-ucits-etf-1c", "Xtrackers MSCI World UCITS ETF 1C"),
        ("s-p-500-equal-weight-swap-ucits-etf-1c", "Xtrackers S&P 500 Equal Weight Swap UCITS ETF 1C"),
        ("s-p-500-ucits-etf-2c-gbp-hedged", "Xtrackers S&P 500 UCITS ETF 2C GBP Hedged"),
        ("eur-overnight-rate-swap-ucits-etf-1d", "Xtrackers EUR Overnight Rate Swap UCITS ETF 1D"),
    ],
)
def test_xtrackers_name_from_slug(slug, expected):
    assert XtrackersListings._name_from_slug(slug) == expected


def test_xtrackers_sitemap():
    xml = """<urlset>
      <url><loc>https://etf.deutsche-am.com/de-de/ueber-uns/ueber-dws/</loc></url>
      <url><loc>https://etf.deutsche-am.com/de-de/IE00BJ0KDQ92-msci-world-ucits-etf-1c/</loc></url>
      <url><loc>https://etf.deutsche-am.com/de-de/LU0779800910-csi300-swap-ucits-etf-1c/</loc></url>
      <url><loc>https://etf.deutsche-am.com/de-de/IE00BJ0KDQ93-not-a-valid-isin/</loc></url>
      <url><loc>https://etf.deutsche-am.com/de-de/IE00BJ0KDQ92-msci-world-ucits-etf-1c/</loc></url>
    </urlset>"""
    df = XtrackersListings.parse_sitemap(xml)
    assert list(df["isin"]) == ["IE00BJ0KDQ92", "LU0779800910"]
    assert list(df["domicile"]) == ["IE", "LU"]
    assert df["fund_name"].iloc[1] == "Xtrackers CSI300 Swap UCITS ETF 1C"
    assert df["ticker"].isna().all()
    assert (df["provider"] == "Xtrackers").all()
