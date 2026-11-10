"""Tests for identifiers, the common holdings format, the HTTP session and fund lookup.

No network access: HTTP behaviour is tested with fake responses.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest
import requests

from etf_scraper import ETFScraper
from etf_scraper.api import load_listings
from etf_scraper.holdings import (
    HOLDINGS_COLUMNS,
    SOURCE_SEC_NPORT,
    check_holdings,
    normalize_holdings,
)
from etf_scraper.http import (
    USER_AGENT,
    ProviderSession,
    UnexpectedResponseError,
    check_content_type,
)
from etf_scraper.identifiers import (
    is_valid_isin,
    isin_check_digit,
    isin_from_cusip,
    looks_like_isin,
)

# ---------------------------------------------------------------- identifiers


@pytest.mark.parametrize(
    ["cusip", "isin"],
    [
        ("037833100", "US0378331005"),  # Apple
        ("922908363", "US9229083632"),  # VOO
        ("46090E103", "US46090E1038"),  # QQQ
        ("78462F103", "US78462F1030"),  # SPY
    ],
)
def test_isin_from_cusip(cusip, isin):
    assert isin_from_cusip(cusip) == isin
    assert is_valid_isin(isin)


@pytest.mark.parametrize(
    "isin",
    ["IE00B5BMR087", "IE00B4L5Y983", "LU0274208692", "DE0005933931"],  # UCITS ETFs
)
def test_valid_european_isins(isin):
    assert is_valid_isin(isin)
    assert isin_check_digit(isin[:11]) == isin[11]


@pytest.mark.parametrize(
    "value", ["US0378331006", "VOO", "", None, "US037833100", "us0378331005x"]
)
def test_invalid_isins(value):
    assert not is_valid_isin(value)


def test_looks_like_isin():
    assert looks_like_isin("IE00B5BMR087")
    assert looks_like_isin("ie00b5bmr087")
    assert not looks_like_isin("VOO")
    assert not looks_like_isin("BRK.B")
    assert isin_from_cusip("bad") is None
    assert isin_from_cusip(np.nan) is None


# ------------------------------------------------------------ holdings format


def _raw_holdings():
    return pd.DataFrame(
        {
            "name": [" Apple Inc ", "Cash", "Microsoft"],
            "ticker": ["AAPL", "", "MSFT"],
            "amount": ["100", "5", None],
            "weight": [60.0, 1.0, 39.0],
            "as_of_date": ["2026-11-09", "2026-11-09", "2026-11-09"],
            "coupon": [np.nan, np.nan, np.nan],  # provider specific column
        }
    )


def test_normalize_holdings_layout():
    df = normalize_holdings(
        _raw_holdings(), fund_ticker="XYZ", fund_isin="US0000000000", provider="Test"
    )

    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert list(df.columns[len(HOLDINGS_COLUMNS):]) == ["coupon"]
    assert (df["fund_ticker"] == "XYZ").all()
    assert (df["provider"] == "Test").all()
    assert (df["source"] == "provider").all()

    assert df["amount"].dtype == "float64"
    assert np.isnan(df["amount"].iloc[2])
    assert df["as_of_date"].dtype == "datetime64[ns]"
    assert df["as_of_date"].iloc[0].date() == date(2026, 11, 9)

    assert df["name"].iloc[0] == "Apple Inc"  # stripped
    assert pd.isna(df["ticker"].iloc[1])  # blank -> missing
    assert df["isin"].isna().all()  # added as empty


def test_normalize_holdings_keeps_input_unchanged():
    raw = _raw_holdings()
    normalize_holdings(raw, fund_ticker="XYZ", source=SOURCE_SEC_NPORT)
    assert list(raw.columns) == list(_raw_holdings().columns)
    assert raw["name"].iloc[0] == " Apple Inc "


def test_check_holdings_flags_problems():
    df = normalize_holdings(_raw_holdings(), fund_ticker="XYZ", check=False)
    assert check_holdings(df) == []

    bad = df.copy()
    bad["weight"] = bad["weight"] / 2  # sums to 50%
    bad.loc[1, "as_of_date"] = pd.Timestamp("2026-11-08")
    warnings = check_holdings(bad)
    assert any("weights sum" in w for w in warnings)
    assert any("as_of_date" in w for w in warnings)

    dupl = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    assert any("duplicated" in w for w in check_holdings(dupl))


# ---------------------------------------------------------------- HTTP session


def _response(content: bytes, content_type: str, status: int = 200, url="https://x.test/f"):
    resp = requests.Response()
    resp.status_code = status
    resp._content = content
    resp.headers["content-type"] = content_type
    resp.url = url
    return resp


def test_check_content_type_accepts_data():
    check_content_type(_response(b'{"a": 1}', "application/json"), ("json",))
    check_content_type(_response(b"a,b\n1,2", "text/csv"), ())


def test_check_content_type_rejects_html():
    # iShares case: labelled as CSV but the body is an HTML page
    html = b"<!DOCTYPE html>\n<html><head></head></html>"
    with pytest.raises(UnexpectedResponseError, match="HTML page"):
        check_content_type(_response(html, "text/csv;charset=UTF-8"), ())
    with pytest.raises(UnexpectedResponseError, match="HTML page"):
        check_content_type(_response(html, "text/html"), ("json",))


def test_check_content_type_rejects_wrong_type():
    with pytest.raises(UnexpectedResponseError, match="content-type"):
        check_content_type(_response(b"a,b", "text/csv"), ("json",))


class _FakeSession:
    """Stands in for requests.Session, returning queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = 0
        self.methods = []

    def request(self, method, url, **kwargs):
        self.calls += 1
        self.methods.append(method)
        return self.responses.pop(0)


def test_provider_session_retries_server_errors(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    session = ProviderSession("Test", min_interval=0)
    fake = _FakeSession(
        [
            _response(b"", "text/plain", status=503),
            _response(b'{"ok": true}', "application/json"),
        ]
    )
    session.session = fake

    resp = session.get("https://x.test/data", expect=("json",))
    assert resp.json() == {"ok": True}
    assert fake.calls == 2


def test_provider_session_post_uses_same_checks(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    session = ProviderSession("Test", min_interval=0)
    fake = _FakeSession(
        [
            _response(b"", "text/plain", status=500),
            _response(b"<!DOCTYPE html><html></html>", "text/html"),
        ]
    )
    session.session = fake

    with pytest.raises(UnexpectedResponseError):
        session.post("https://x.test/api", json={"a": 1}, expect=("json",))
    assert fake.methods == ["POST", "POST"]


def test_provider_session_does_not_retry_client_errors(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    session = ProviderSession("Test", min_interval=0)
    fake = _FakeSession([_response(b"", "text/plain", status=404)])
    session.session = fake

    with pytest.raises(requests.HTTPError):
        session.get("https://x.test/missing")
    assert fake.calls == 1


def test_provider_session_identifies_honestly():
    session = ProviderSession("Test", headers={"Accept": "application/json"})
    ua = session.session.headers["User-Agent"]
    assert ua == USER_AGENT
    assert ua.startswith("etf-scraper/")
    assert "Mozilla" not in ua
    assert session.session.headers["Accept"] == "application/json"


# ----------------------------------------------------------------- fund lookup


def test_bundled_listings_isins_are_valid():
    listings = load_listings()
    isins = listings["isin"].dropna()
    assert isins.map(is_valid_isin).all()
    assert not isins.duplicated().any()
    # domicile matches the ISIN country, except for international "XS" ISINs
    # (exchange traded commodities/notes, eg Invesco Physical Gold) which have none
    with_isin = listings.dropna(subset=["isin"])
    national = with_isin[~with_isin["isin"].str.startswith("XS")]
    assert (national["domicile"] == national["isin"].str[:2]).all()
    us_providers = {"IShares", "SSGA", "Vanguard", "Invesco"}
    assert (listings.loc[listings["provider"].isin(us_providers), "domicile"] == "US").all()
    # leading zeros preserved
    assert listings.loc[listings["ticker"] == "VOO", "product_id"].iloc[0] == "0968"


def test_find_listing_by_ticker_and_isin():
    es = ETFScraper()
    by_ticker = es._find_sec_listing("voo")
    by_isin = es._find_sec_listing("US9229083632")
    # compare identifying fields (empty fields are NaN, and NaN != NaN)
    key = lambda s: (s.ticker, s.isin, s.cusip, s.provider, s.product_id)
    assert key(by_ticker) == key(by_isin) == ("VOO", "US9229083632", "922908363", "Vanguard", "0968")
    assert by_ticker.domicile == "US"


def test_find_listing_ambiguous_ticker():
    listings = pd.DataFrame(
        {
            "ticker": ["ABC", "ABC"],
            "isin": ["IE00B5BMR087", "LU0274208692"],
            "provider": ["IShares_EU", "Xtrackers"],
            "exchange": ["XETRA", "LSE"],
            "fund_name": ["Fund A", "Fund B"],
        }
    )
    es = ETFScraper(listings)

    with pytest.raises(ValueError, match="Multiple listings"):
        es._find_sec_listing("ABC")
    assert es._find_sec_listing("ABC", provider="Xtrackers").isin == "LU0274208692"
    assert es._find_sec_listing("ABC", exchange="xetra").provider == "IShares_EU"
    assert es._find_sec_listing("IE00B5BMR087").exchange == "XETRA"

    with pytest.raises(ValueError, match="No listings found for ISIN"):
        es._find_sec_listing("DE0005933931")


def test_find_listing_prefers_us_fund_for_bare_ticker():
    """Tickers used to mean US funds: keep that when a UCITS fund shares the ticker"""
    listings = pd.DataFrame(
        {
            "ticker": ["GOVT", "GOVT", "XYZ", "XYZ"],
            "isin": ["US4642876142", "IE00B1FZS798", "IE00B5BMR087", "IE00B4L5Y983"],
            "provider": ["IShares", "IShares_EU", "IShares_EU", "IShares_EU"],
            "domicile": ["US", "IE", "IE", "IE"],
            "fund_name": ["US fund", "UCITS fund", "A", "B"],
        }
    )
    es = ETFScraper(listings)

    assert es._find_sec_listing("GOVT").provider == "IShares"
    assert es._find_sec_listing("GOVT", provider="IShares_EU").isin == "IE00B1FZS798"
    assert es._find_sec_listing("IE00B1FZS798").provider == "IShares_EU"

    # no US fund among the matches: still ambiguous
    with pytest.raises(ValueError, match="Multiple listings"):
        es._find_sec_listing("XYZ")


def test_bundled_listings_keep_us_tickers_working():
    """Bare US tickers that iShares EU funds also use still resolve to the US fund"""
    es = ETFScraper()
    for ticker in ["GOVT", "FLOT", "SPMV", "CNYA"]:
        assert es._find_sec_listing(ticker).domicile == "US"
    cspx = es._find_sec_listing("CSPX")
    assert (cspx.provider, cspx.isin) == ("IShares_EU", "IE00B5BMR087")
