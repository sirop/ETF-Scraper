"""Tests for the HSBC UCITS ETF scraper. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import HSBCListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")
XLS = DATA_DIR.joinpath("HSBC_IE00B42TW061_holdings_20261105.xls")  # HSBC FTSE 100 UCITS ETF

pytestmark = pytest.mark.filterwarnings("ignore:The xlrd engine is deprecated")


def _page(isin, holdings=True, title="HSBC FTSE 100 UCITS ETF"):
    link = (
        f'<a href="/api/v1/download/document/{isin.lower()}/de/de/holdings"></a>' if holdings else ""
    )
    return (
        f"<html><head><title>{title}</title></head><body><script>"
        f"window.HSBC.dpas = {{ pageInformation: {{ investorType: 'INDV', shareId: '{isin}' }} }};"
        f"</script><noscript>{link}</noscript></body></html>"
    )


def test_hsbc_holdings_parser():
    df = HSBCListings._parse_holdings_resp(XLS.read_bytes())
    assert len(df) == 123
    assert (df["as_of_date"] == pd.Timestamp("2026-11-05")).all()
    assert np.isclose(df["weight"].sum(), 99.99998)
    first = df.iloc[0]
    assert (first["name"], first["isin"], first["amount"], first["market_value"]) == (
        "HSBC Holdings PLC", "GB0005405286", 5777038.0, 75528995
    )
    assert first["fund_name"] == "HSBC FTSE 100 UCITS ETF"
    assert df.loc[2, "cusip"] == "G0593M107"  # AstraZeneca
    # tax receivables and payables at the end have no ISIN
    assert pd.isna(df["isin"].iloc[-1]) and df["name"].iloc[-1] == "Gbp Distribution Payable"


def test_hsbc_holdings_parser_rejects_other_files():
    import io

    buf = io.BytesIO()
    pd.DataFrame({"a": ["no", "table"]}).to_excel(buf, index=False, header=False)
    with pytest.raises(InvalidParameterError, match="no holdings table"):
        HSBCListings._parse_holdings_resp(buf.getvalue())


def test_hsbc_query_holdings_normalized(monkeypatch):
    calls = []

    class _Resp:
        content = XLS.read_bytes()

    def fake_get(url, expect=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(HSBCListings.session, "get", fake_get)
    listing = SecurityListing(ticker="", provider="HSBC", isin="IE00B42TW061")
    df = HSBCListings.query_holdings(listing, None)
    assert calls == [
        "https://www.assetmanagement.hsbc.de/api/v1/download/document/ie00b42tw061/de/de/holdings"
    ]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_isin"] == "IE00B42TW061").all()
    assert df["fund_ticker"].isna().all()  # HSBC tickers aren't known
    assert (df["provider"] == "HSBC").all()


def test_hsbc_latest_only():
    listing = SecurityListing(ticker="", provider="HSBC", isin="IE00B42TW061")
    with pytest.raises(InvalidParameterError, match="latest"):
        HSBCListings._retrieve_holdings(listing, date(2026, 10, 31))


@pytest.mark.parametrize(
    "value, expected",
    [
        (pd.Timestamp("2026-11-05"), pd.Timestamp("2026-11-05")),
        ("05.11.2026", pd.Timestamp("2026-11-05")),
        ("2026-11-05", pd.Timestamp("2026-11-05")),
        (46331, pd.Timestamp("2026-11-05")),  # Excel serial date
        (None, None),
    ],
)
def test_hsbc_header_date(value, expected):
    assert HSBCListings._parse_header_date(value) == expected


def test_hsbc_fund_page_parser():
    isin = "IE00B42TW061"
    assert HSBCListings.parse_fund_page(isin, _page(isin)) == {
        "page_name": "HSBC FTSE 100 UCITS ETF", "has_holdings": True
    }
    assert HSBCListings.parse_fund_page(isin, _page(isin, holdings=False))["has_holdings"] is False
    # a closed share class redirects to the fund list, which has no shareId
    assert HSBCListings.parse_fund_page(isin, "<html><title>Fonds</title></html>") is None
    assert HSBCListings.parse_fund_page(isin, _page("IE00B4X9L533")) is None


def test_hsbc_seed_file():
    seed = HSBCListings.load_seed()
    assert len(seed) >= 80
    assert seed["isin"].str.startswith("IE").all()
    assert "IE00B42TW061" in set(seed["isin"])


def test_hsbc_listings(monkeypatch):
    seed = pd.DataFrame(
        {
            "isin": ["IE00B42TW061", "IE00B4X9L533", "IE000JZ473P7"],
            "fund_name": ["HSBC FTSE 100 UCITS ETF", "", "HSBC S&P 500 UCITS ETF USD (Acc)"],
        }
    )
    pages = {
        "ie00b42tw061": _page("IE00B42TW061"),
        "ie00b4x9l533": _page("IE00B4X9L533", title="HSBC MSCI WORLD UCITS ETF"),
        "ie000jz473p7": "<html><title>Fonds</title></html>",  # closed: no fund page
    }

    class _Resp:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(
        HSBCListings.session, "get", lambda url, expect=None: _Resp(pages[url.rsplit("/", 1)[-1]])
    )
    listings = HSBCListings.retrieve_listings(seed)
    assert listings["isin"].tolist() == ["IE00B42TW061", "IE00B4X9L533"]
    assert listings["fund_name"].tolist() == ["HSBC FTSE 100 UCITS ETF", "HSBC MSCI WORLD UCITS ETF"]
    assert (listings["provider"] == "HSBC").all()
    assert listings["ticker"].isna().all()
