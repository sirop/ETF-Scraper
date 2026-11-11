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
