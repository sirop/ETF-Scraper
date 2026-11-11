"""Tests for importing holdings files downloaded by hand. No network access."""

import os
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from etf_scraper import import_holdings_file
from etf_scraper.holdings import HOLDINGS_COLUMNS

DATA_DIR = Path(__file__).parent.joinpath("test_data")
WT_FILE = DATA_DIR.joinpath("WisdomTree_fund-holdings-1000492_20261111.csv")


def test_wisdomtree_import():
    df = import_holdings_file(
        "wisdomtree", WT_FILE, fund_isin="ie00example1", fund_ticker="TEST",
        as_of_date="2026-11-11",
    )
    assert list(df.columns[: len(HOLDINGS_COLUMNS)]) == HOLDINGS_COLUMNS
    assert len(df) == 99
    first = df.iloc[0]
    assert (first["name"], first["sector"], first["weight"]) == ("L'Oreal SA", "Consumer Staples", 5.86)
    assert np.isclose(df["weight"].sum(), 99.98)
    assert (df["source"] == "manual_download").all()
    assert (df["provider"] == "WisdomTree").all()
    assert (df["fund_isin"] == "IE00EXAMPLE1").all()
    assert (df["fund_ticker"] == "TEST").all()
    assert (df["as_of_date"] == pd.Timestamp("2026-11-11")).all()


def test_import_uses_file_date(tmp_path):
    path = tmp_path / "fund-holdings.csv"
    shutil.copy(WT_FILE, path)
    stamp = datetime(2026, 10, 30, 15, 0).timestamp()
    os.utime(path, (stamp, stamp))
    df = import_holdings_file("WisdomTree", path)
    assert (df["as_of_date"] == pd.Timestamp("2026-10-30")).all()


def test_import_errors(tmp_path):
    with pytest.raises(ValueError, match="No manual import"):
        import_holdings_file("Nobody", WT_FILE)
    other = tmp_path / "other.csv"
    other.write_text("Ticker,Price\nABC,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Not a WisdomTree holdings file"):
        import_holdings_file("WisdomTree", other)


def test_wisdomtree_fund_from_file_name(tmp_path):
    path = tmp_path / "fund-holdings-1000490.csv"
    shutil.copy(WT_FILE, path)
    df = import_holdings_file("WisdomTree", path, as_of_date="2026-11-11")
    assert (df["fund_isin"] == "IE00BZ56TQ67").all()
    assert (df["fund_ticker"] == "EGRA").all()
    assert df["fund_name"].iloc[0] == "WisdomTree Eurozone Quality Dividend Growth UCITS ETF - EUR Acc"


def test_wisdomtree_unknown_fund_number(tmp_path, caplog):
    path = tmp_path / "fund-holdings-9999999.csv"
    shutil.copy(WT_FILE, path)
    df = import_holdings_file("WisdomTree", path, as_of_date="2026-11-11")
    assert df["fund_isin"].isna().all()
    assert "unknown fund" in caplog.text


# structure as on the saved EGRA page (2026-11-11), shortened
WT_PAGE = (
    '<h1><span class="x" role="button" tabindex="0">EGRA</span><span class="typ-h3">'
    'WisdomTree Eurozone Quality Dividend Growth UCITS ETF - EUR Acc</span></h1>'
    '<span class="typ-body-2m" role="button" tabindex="0">ISIN: IE00BZ56TQ67<svg></svg></span>'
    '<a href="https://www.wisdomtree.eu/x/1000490.product/IwZnF-MeAJrj/download?locale=en-IE">a</a>'
    '<a href="https://www.wisdomtree.eu/x/1000490.product/MG2LpMOD/download?locale=en-IE">b</a>'
    '<script>self.__next_f.push([1,"{\\"siteSlug\\":\\"wisdomtree-eu\\",'
    '\\"productId\\":\\"1000492.product\\",\\"isin\\":\\"IE00BZ56SY76\\",'
    '\\"issuerShortCode\\":\\"UCITS\\",\\"productName\\":\\"WisdomTree Eurozone Quality '
    'Dividend Growth UCITS ETF - EUR\\",\\"ticker\\":\\"EGRW\\"}"])</script>'
    '<a href="https://www.wisdomtree.eu/x/1000492.product/SI7uBgo4/download?locale=en-IE">c</a>'
)


def test_wisdomtree_funds_from_page():
    from etf_scraper.manual_import import wisdomtree_funds_from_page

    df = wisdomtree_funds_from_page(WT_PAGE)
    rows = {r["fund_id"]: r for r in df.to_dict("records")}
    assert rows["1000492"] == {
        "fund_id": "1000492", "isin": "IE00BZ56SY76", "ticker": "EGRW",
        "fund_name": "WisdomTree Eurozone Quality Dividend Growth UCITS ETF - EUR",
    }
    assert rows["1000490"] == {
        "fund_id": "1000490", "isin": "IE00BZ56TQ67", "ticker": "EGRA",
        "fund_name": "WisdomTree Eurozone Quality Dividend Growth UCITS ETF - EUR Acc",
    }
