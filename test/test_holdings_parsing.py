"""
Tests we correctly parse responses for holdings data from different providers.

#TODO: these aren't comprehensive tests (so far), we're just testing things work as intended. In particular,
we are missing tests for when responses aren't as intended, though I haven't had problems with this so far.
"""

from datetime import date
import json
from pathlib import Path

import pytest
import numpy as np
import pandas as pd

from etf_scraper.edgar import parse_nport_xml
from etf_scraper.scrapers import (
    ISharesListings,
    InvescoListings,
    SSGAListings,
    VanguardListings,
)

DATA_DIR = Path(__file__).parent.joinpath("test_data")


def _read_file(filename, read_mode):
    resp_file = str(DATA_DIR.joinpath(filename))
    encoding = None if "b" in read_mode else "utf-8"  # Windows defaults to cp1252
    with open(resp_file, read_mode, encoding=encoding) as f:
        return f.read()


@pytest.fixture
def ishares_test_resp():
    return _read_file("IShares_IVV_holdings_20221230_resp.csv", "r")


@pytest.fixture
def ishares_test_dupl_resp():
    return _read_file("IShares_HEWU_holdings_20230121.csv", "r")


@pytest.fixture
def ssga_test_resp():
    return _read_file("SSGA_SPY_holdings_20230110_resp.xlsx", "rb")


@pytest.fixture
def vanguard_test_resp():
    return json.loads(
        _read_file("Vanguard_VOO_stock_holdings_20260930_resp.json", "r")
    )


@pytest.fixture
def vanguard_str_test_resp():
    return json.loads(
        _read_file("Vanguard_VOO_short-term-reserve_holdings_20260930_resp.json", "r")
    )


@pytest.fixture
def vanguard_bond_test_resp():
    return json.loads(
        _read_file("Vanguard_BND_bond_holdings_20260930_resp_page1.json", "r")
    )


@pytest.fixture
def invesco_test_resp():
    return json.loads(_read_file("Invesco_QQQ_holdings_20261109_resp.json", "r"))


@pytest.fixture
def nport_test_resp():
    return _read_file("NPORT_QQQ_20260630_primary_doc.xml", "rb")


def check_holdings_df(
    holdings_df,
    exp_tot_holdings,
    exp_tot_mkt_val,
    exp_first_ticker,
    exp_last_ticker,
    exp_weight,
):
    """Perform some basic checks on the parsed responses for holdings dfs:
    - Check the total # of shares is as expected
    - Check the first and last tickers match
        - # BUG: doesn't work if the parser also sorts, but this doesn't happen
    - Check the sum of weights is as expected (should be close to 1, but will be off due
    to rounding errors)
    """
    act_tot_holdings = holdings_df["amount"].sum()
    assert act_tot_holdings == exp_tot_holdings  # integer so need exact match

    if exp_first_ticker:
        act_first_ticker = holdings_df["ticker"].iloc[0]
        assert act_first_ticker == exp_first_ticker

    if exp_last_ticker:
        act_last_ticker = holdings_df["ticker"].iloc[-1]
        assert act_last_ticker == exp_last_ticker

    # float values so need np.isclose
    if exp_tot_mkt_val is not None:
        assert np.isclose(holdings_df["market_value"].sum(), exp_tot_mkt_val)

    if exp_weight:
        assert np.isclose(holdings_df["weight"].sum(), exp_weight)


def test_ishares_etf_holdings_resp_parser(ishares_test_resp):
    # exp_ticker = "IVV" # IShares doesn't include fund ticker in the response file
    exp_holdings_date = date(year=2022, month=12, day=30)

    # calculated directly from the file
    exp_tot_holdings = 3690353839
    exp_tot_mkt_val = 289404832047
    exp_weight = 100.02
    exp_first_ticker = "AAPL"
    exp_last_ticker = "ETD_USD"

    # with open(resp_file, "r") as f:
    holdings_df, act_holdings_date = ISharesListings._parse_holdings_resp(
        ishares_test_resp
    )

    assert act_holdings_date == exp_holdings_date

    check_holdings_df(
        holdings_df,
        exp_tot_holdings,
        exp_tot_mkt_val,
        exp_first_ticker,
        exp_last_ticker,
        exp_weight,
    )


def test_ssga_etf_holdings_resp_parser(ssga_test_resp):

    exp_ticker = "SPY"
    exp_holdings_date = date(year=2023, month=1, day=10)

    # calculated directly from the file
    exp_tot_holdings = 3731709239
    exp_tot_mkt_val = None  # not provided in the file
    exp_weight = 99.999959
    exp_first_ticker = "AAPL"
    exp_last_ticker = "NWS"

    holdings_df, act_holdings_date, act_ticker = SSGAListings._parse_holdings_resp(
        ssga_test_resp
    )

    assert act_ticker == exp_ticker
    assert act_holdings_date == exp_holdings_date

    check_holdings_df(
        holdings_df,
        exp_tot_holdings,
        exp_tot_mkt_val,
        exp_first_ticker,
        exp_last_ticker,
        exp_weight,
    )


def _as_dates(series):
    """Normalise a date/Timestamp column to a list of unique datetime.date"""
    return sorted({pd.Timestamp(x).date() for x in series.dropna().unique()})


def test_vanguard_etf_holdings_resp_parser(vanguard_test_resp):
    """VOO stock holdings from investor.vanguard.com/vmf/api/VOO/portfolio-holding/stock.json"""
    exp_holdings_date = date(year=2026, month=9, day=30)

    # calculated directly from the file
    exp_tot_holdings = 9633605676
    exp_tot_mkt_val = 1730342128089.43
    exp_weight = 100.03

    holdings_df, as_of_date = VanguardListings._parse_holdings_resp(
        vanguard_test_resp, "stock"
    )

    assert as_of_date.date() == exp_holdings_date
    assert _as_dates(holdings_df["as_of_date"]) == [exp_holdings_date]
    assert len(holdings_df) == 503
    assert set(holdings_df["holding_category"]) == {"stock"}
    assert holdings_df["weight"].dtype.kind == "f"

    check_holdings_df(
        holdings_df,
        exp_tot_holdings,
        exp_tot_mkt_val,
        exp_first_ticker="NVDA",
        exp_last_ticker="NWS",
        exp_weight=exp_weight,
    )


def test_vanguard_short_term_reserve_resp_parser(vanguard_str_test_resp):
    """Cash positions have blank tickers, which should become NaN"""
    holdings_df, _ = VanguardListings._parse_holdings_resp(
        vanguard_str_test_resp, "short-term-reserve"
    )
    assert len(holdings_df) == 2
    assert holdings_df["ticker"].isna().all()
    assert holdings_df["amount"].sum() == 887214214
    assert np.isclose(holdings_df["market_value"].sum(), 893213780.89)


def test_vanguard_bond_resp_parser(vanguard_bond_test_resp):
    """Bond holdings include coupon/maturity columns. This is the first page only."""
    holdings_df, as_of_date = VanguardListings._parse_holdings_resp(
        vanguard_bond_test_resp, "bond"
    )
    assert as_of_date.date() == date(year=2026, month=9, day=30)
    assert len(holdings_df) == 200
    assert np.isclose(holdings_df["market_value"].sum(), 219143253334.46)
    assert holdings_df["coupon"].notna().any()
    assert holdings_df["maturity_date"].notna().any()


def test_vanguard_empty_category_resp_parser():
    """A category the fund doesn't hold comes back with size 0 and no entities"""
    resp = {"size": 0, "asOfDate": "2026-09-30T00:00:00-04:00"}
    holdings_df, as_of_date = VanguardListings._parse_holdings_resp(resp, "bond")
    assert holdings_df.empty
    assert as_of_date.date() == date(year=2026, month=9, day=30)


def test_invesco_etf_holdings_resp_parser(invesco_test_resp):
    """QQQ holdings from dng-api.invesco.com/.../shareclasses/46090E103/holdings/fund"""
    exp_holdings_date = date(year=2026, month=11, day=9)

    # calculated directly from the file
    exp_tot_holdings = 3008502642.2431
    exp_weight = 100.000003
    exp_tot_mkt_val = 463824631864.85

    holdings_df, as_of_date, cusip = InvescoListings._parse_holdings_resp(
        invesco_test_resp
    )

    assert cusip == "46090E103"
    assert as_of_date.date() == exp_holdings_date
    assert _as_dates(holdings_df["as_of_date"]) == [exp_holdings_date]
    assert len(holdings_df) == 106

    assert np.isclose(holdings_df["amount"].sum(), exp_tot_holdings)
    check_holdings_df(
        holdings_df,
        holdings_df["amount"].sum(),  # float units, checked approximately above
        exp_tot_mkt_val,
        exp_first_ticker="AAPL",
        exp_last_ticker="USDPDV",
        exp_weight=exp_weight,
    )


def test_nport_holdings_parser(nport_test_resp):
    """QQQ N-PORT filing (primary_doc.xml) for 2026-06-30 from SEC EDGAR"""
    holdings_df = parse_nport_xml(nport_test_resp)

    assert len(holdings_df) == 105
    assert _as_dates(holdings_df["as_of_date"]) == [date(year=2026, month=6, day=30)]
    assert set(holdings_df["series_id"]) == {"S000101292"}
    assert np.isclose(holdings_df["net_assets"].iloc[0], 490103179941.66)

    assert np.isclose(holdings_df["amount"].sum(), 3948756511.2031)
    assert np.isclose(holdings_df["market_value"].sum(), 491485797052.9205)
    assert np.isclose(holdings_df["weight"].sum(), 100.282107353626)

    assert holdings_df["cusip"].iloc[0] == "G25839104"
    assert holdings_df["cusip"].iloc[-1] == "02079K107"
    # cash sweep funds and the futures position have no ISIN
    assert holdings_df["isin"].isna().sum() == 3

    # futures are reported with name "N/A", the parser falls back to the title
    futures = holdings_df[holdings_df["asset_category"] == "DE"]
    assert list(futures["name"]) == ["CME E-Mini NASDAQ 100 Index Future"]
    assert list(futures["ticker"]) == ["NQU6"]


def test_ishares_dupl_holdings_resp_parser(ishares_test_dupl_resp):
    # exp_ticker = "HEWU" # IShares doesn't include fund ticker in the response file
    exp_holdings_date = date(year=2023, month=1, day=20)

    # calculated directly from the file
    exp_tot_holdings = -10921410
    exp_tot_mkt_val = 14220570.81
    exp_weight = 100
    exp_first_ticker = "EWU"
    exp_last_ticker = "GBP"

    # with open(resp_file, "r") as f:
    holdings_df, act_holdings_date = ISharesListings._parse_holdings_resp(
        ishares_test_dupl_resp
    )

    assert act_holdings_date == exp_holdings_date

    check_holdings_df(
        holdings_df,
        exp_tot_holdings,
        exp_tot_mkt_val,
        exp_first_ticker,
        exp_last_ticker,
        exp_weight,
    )
