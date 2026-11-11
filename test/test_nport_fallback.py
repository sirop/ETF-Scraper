"""Tests for the automatic SEC N-PORT fallback in ETFScraper.query_holdings. No network access."""

from datetime import date

import pandas as pd
import pytest

from etf_scraper import ETFScraper, api, edgar
from etf_scraper.base import InvalidParameterError
from etf_scraper.holdings import HOLDINGS_COLUMNS
from etf_scraper.scrapers import ISharesEUListings, ISharesListings

UA = "Test Person test@example.com"


def _listings():
    return pd.DataFrame([
        {"ticker": "IVV", "isin": "US4642872000", "provider": "IShares",
         "product_url": "https://example.test/ivv", "domicile": "US"},
        {"ticker": "CSPX", "isin": "IE00B5BMR087", "provider": "IShares_EU",
         "product_url": "https://example.test/cspx", "domicile": "IE"},
    ])


def _nport_df(ticker, holdings_date):
    return pd.DataFrame({
        "name": ["APPLE INC", "MICROSOFT CORP"],
        "isin": ["US0378331005", "US5949181045"],
        "weight": [60.0, 40.0],
        "fund_ticker": ticker,
        "as_of_date": pd.Timestamp(holdings_date),
        "source": "sec_nport",
    })


@pytest.fixture
def provider_latest_only(monkeypatch):
    """Both iShares scrapers only have the latest holdings"""
    def fail(sec_listing, holdings_date=None):
        raise InvalidParameterError("only the latest holdings")

    monkeypatch.setattr(ISharesListings, "_retrieve_holdings", fail)
    monkeypatch.setattr(ISharesEUListings, "_retrieve_holdings", fail)


@pytest.fixture
def nport_calls(monkeypatch):
    calls = []

    def fake_nport(ticker, holdings_date=None, user_agent=None, **kwargs):
        calls.append((ticker, holdings_date, user_agent))
        return _nport_df(ticker, holdings_date)

    monkeypatch.setattr(api, "query_nport_holdings", fake_nport)
    return calls


def test_fallback_for_us_fund(provider_latest_only, nport_calls, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    df = ETFScraper(_listings()).query_holdings("IVV", "2026-06-30")

    assert nport_calls == [("IVV", date(2026, 6, 30), UA)]
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert (df["source"] == "sec_nport").all()
    assert (df["provider"] == "IShares").all()
    assert (df["fund_isin"] == "US4642872000").all()
    assert df["as_of_date"].iloc[0] == pd.Timestamp("2026-06-30")


def test_user_agent_argument(provider_latest_only, nport_calls, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    ETFScraper(_listings()).query_holdings(
        "IVV", date(2026, 6, 30), sec_user_agent="Other other@example.com"
    )
    assert nport_calls[0][2] == "Other other@example.com"


def test_no_user_agent_keeps_provider_error(provider_latest_only, nport_calls, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(InvalidParameterError, match="only the latest.*SEC_USER_AGENT"):
        ETFScraper(_listings()).query_holdings("IVV", date(2026, 6, 30))
    assert nport_calls == []


def test_no_fallback_when_disabled(provider_latest_only, nport_calls, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    with pytest.raises(InvalidParameterError, match="only the latest"):
        ETFScraper(_listings()).query_holdings(
            "IVV", date(2026, 6, 30), nport_fallback=False
        )
    assert nport_calls == []


def test_no_fallback_for_ucits_fund(provider_latest_only, nport_calls, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    with pytest.raises(InvalidParameterError, match="only the latest"):
        ETFScraper(_listings()).query_holdings("CSPX", date(2026, 6, 30))
    assert nport_calls == []


def test_no_fallback_for_latest(monkeypatch, nport_calls):
    """Errors for the latest holdings (eg a closed fund) aren't N-PORT's business"""
    def fail(sec_listing, holdings_date=None):
        raise InvalidParameterError("no holdings download")

    monkeypatch.setattr(ISharesListings, "_retrieve_holdings", fail)
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    with pytest.raises(InvalidParameterError, match="no holdings download"):
        ETFScraper(_listings()).query_holdings("IVV")
    assert nport_calls == []


def test_both_errors_reported(provider_latest_only, monkeypatch):
    def fake_nport(ticker, holdings_date=None, user_agent=None, **kwargs):
        raise InvalidParameterError("No public N-PORT filing for IVV as of 2026-06-15")

    monkeypatch.setattr(api, "query_nport_holdings", fake_nport)
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    with pytest.raises(InvalidParameterError, match="only the latest.*N-PORT fallback"):
        ETFScraper(_listings()).query_holdings("IVV", date(2026, 6, 15))


def test_filing_report_dates_cached(monkeypatch):
    """The index headers of a filing are only downloaded once"""
    edgar._filing_report_date.cache_clear()
    requests_made = []

    class _Resp:
        text = "<html>CONFORMED PERIOD OF REPORT:\t20260630</html>"

    def fake_sec_get(url, user_agent=None, **kwargs):
        requests_made.append(url)
        return _Resp()

    monkeypatch.setattr(edgar, "_sec_get", fake_sec_get)
    folder = "https://www.sec.gov/Archives/edgar/data/1/000000000026000001/"
    for _ in range(3):
        d = edgar._filing_report_date(folder, "0000000000-26-000001", UA)
    assert d == date(2026, 6, 30)
    assert len(requests_made) == 1
    edgar._filing_report_date.cache_clear()
