"""Tests for the L&G scraper and the opt-in download helper. No network access."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import ETFScraper, optin
from etf_scraper.base import InvalidParameterError, SecurityListing
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import LGIMListings

DATA_DIR = Path(__file__).parent.joinpath("test_data")
CSV = DATA_DIR.joinpath("LGIM_IE00BK5BCD43_Fundholdings_20261111.csv")
PAGE = DATA_DIR.joinpath("LGIM_IE00BK5BCD43_page_20261111.html")
PAGE_URL = (
    "https://fundcentres.landg.com/en/de/private-investors/fund-centre/ETF/"
    "Artificial-Intelligence/IE00BK5BCD43/"
)
CSV_URL = (
    "https://fundcentres.lgim.com/srp/documents-id/"
    "8fb44abf-9bb1-44ef-b758-775816534f9c/Fundholdings.csv"
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(optin.CACHE_ENV, str(tmp_path))
    monkeypatch.delenv(optin.OPT_IN_ENV, raising=False)
    return tmp_path


class _Resp:
    def __init__(self, text=None, content=None):
        self.text = text
        self.content = content


@pytest.fixture
def fake_site(monkeypatch):
    calls = []

    def fake_get(url, expect=None):
        calls.append(url)
        if url == PAGE_URL:
            return _Resp(text=PAGE.read_text(encoding="utf-8"))
        if url == CSV_URL:
            return _Resp(content=CSV.read_bytes())
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(LGIMListings.session, "get", fake_get)
    return calls


def test_lgim_holdings_parser():
    df = LGIMListings._parse_holdings_resp(CSV.read_text(encoding="utf-8"))
    assert len(df) == 55
    first = df.iloc[0]
    assert (first["name"], first["isin"], first["currency"]) == (
        "ADVANCED MICRO DEVICES INC USD 0.01", "US0079031078", "USD"
    )
    assert np.isclose(first["weight"], 2.4363768807)  # fraction converted to percent
    assert np.isclose(df["weight"].sum(), 100.0)
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-11-11")
    assert df["basket_isin"].iloc[0] == "IE00BK5BCD43"
    assert df["fund_name"].iloc[0] == "L&G Artificial Intelligence UCITS ETF"
    assert df["name"].iloc[-1] == "SHOPIFY INC NPV"  # notes and disclaimer not included


def test_lgim_portfolio_composition_file():
    """Synthetic funds (eg All Commodities) publish a full PCF with futures per basket"""
    df = LGIMListings._parse_holdings_resp(
        DATA_DIR.joinpath("LGIM_IE00BF0BCP69_Fundholdings_PCF_20261111.csv").read_text(encoding="utf-8")
    )
    assert len(df) == 50
    first = df.iloc[0]
    assert first["name"] == "CBT Chicago Board of Trade Corn Electronic Commodity Future"
    assert (first["ticker"], first["security_type"], first["currency"]) == ("C H7", "Future", "USD")
    # 0.863396 contracts x 4.615 x 5000 per basket of 50,000 shares (NAV 20.0469 per share)
    assert np.isclose(first["weight"], 100 * 0.863396 * 4.615 * 5000 / (20.046853487882768 * 50000))
    assert np.isclose(first["amount"], 0.863396 * 481.54574)  # scaled to the fund
    assert np.isclose(df["weight"].sum(), 100.0, atol=0.01)
    # fund level market value matches the reported net assets (482,673,845 USD)
    assert np.isclose(df["market_value"].sum(), 482673844.87, rtol=1e-5)
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-11-11")
    assert df["basket_isin"].iloc[0] == "IE00BF0BCP69"


def test_lgim_rejects_other_files():
    with pytest.raises(InvalidParameterError, match="no holdings table"):
        LGIMListings._parse_holdings_resp("<html>not a basket file</html>")


def test_lgim_needs_opt_in(fake_site):
    with pytest.raises(InvalidParameterError, match="ETF_SCRAPER_OPT_IN=LGIM"):
        LGIMListings.retrieve_holdings("IE00BK5BCD43", PAGE_URL)
    assert fake_site == []  # nothing downloaded without the opt-in


def test_lgim_opt_in_download_and_daily_cache(fake_site, monkeypatch, isolated_cache):
    monkeypatch.setenv(optin.OPT_IN_ENV, "Xtrackers, lgim")  # case and spaces don't matter
    listings = pd.DataFrame([{
        "ticker": "XMLD", "isin": "IE00BK5BCD43", "provider": "LGIM",
        "product_url": PAGE_URL, "domicile": "IE",
    }])
    scraper = ETFScraper(listings)
    df = scraper.query_holdings("IE00BK5BCD43")
    assert fake_site == [PAGE_URL, CSV_URL]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["fund_ticker"] == "XMLD").all()
    assert (df["provider"] == "LGIM").all()
    assert len(df) == 55

    # a second query on the same day is served from the cache
    scraper.query_holdings("IE00BK5BCD43")
    assert fake_site == [PAGE_URL, CSV_URL]
    assert len(list((isolated_cache / "LGIM").glob("IE00BK5BCD43_*.csv"))) == 1


def test_lgim_latest_only():
    listing = SecurityListing(ticker="XMLD", provider="LGIM", isin="IE00BK5BCD43",
                              product_url=PAGE_URL)
    with pytest.raises(InvalidParameterError, match="latest"):
        LGIMListings._retrieve_holdings(listing, date(2026, 10, 31))


def test_lgim_fund_page_parser():
    info = LGIMListings.parse_fund_page(PAGE.read_text(encoding="utf-8"))
    assert info == {
        "fund_name": "L&G Artificial Intelligence UCITS ETF",
        "asset_class": "Equity",
        "isin": "IE00BK5BCD43",
        "ticker": "XMLD",  # the Xetra listing is preferred
        "exchange": "Deutsche Börse Xetra",
        "currency": "USD",
    }
    assert LGIMListings.parse_fund_page("<html></html>") is None


def test_lgim_sitemap():
    base = "https://fundcentres.landg.com/en/de/private-investors/fund-centre/ETF/"
    xml = f"""<urlset>
      <url><loc>{base}Artificial-Intelligence/IE00BK5BCD43/</loc></url>
      <url><loc>{base}Healthcare-Technology-&amp;-Innovation/IE00BK5BC677/</loc></url>
      <url><loc>{base}FTSE-100-Super-Short-Strategy-Daily-2x/</loc></url>
      <url><loc>https://fundcentres.landg.com/en/de/private-investors/insights/</loc></url>
    </urlset>"""
    assert LGIMListings.parse_sitemap(xml) == [
        base + "Artificial-Intelligence/IE00BK5BCD43/",
        base + "Healthcare-Technology-&-Innovation/IE00BK5BC677/",
    ]


def test_optin_helpers(monkeypatch, isolated_cache):
    assert not optin.opted_in("LGIM")
    monkeypatch.setenv(optin.OPT_IN_ENV, "LGIM")
    assert optin.opted_in("LGIM") and not optin.opted_in("UBS")

    downloads = []

    def download():
        downloads.append(1)
        return b"data"

    day1, day2 = date(2026, 11, 11), date(2026, 11, 12)
    assert optin.cached_daily_download("P", "K", download, today=day1) == b"data"
    optin.cached_daily_download("P", "K", download, today=day1)
    assert len(downloads) == 1
    optin.cached_daily_download("P", "K", download, today=day2)
    assert len(downloads) == 2
    files = sorted(p.name for p in (isolated_cache / "P").iterdir())
    assert files == ["K_20261112.csv"]  # yesterday's file was removed
