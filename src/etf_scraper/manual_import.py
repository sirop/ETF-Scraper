"""Import holdings files a user downloaded in their own browser.

Some providers block automated access (eg bot challenges), so their holdings are
not fetched by this library. Their fund pages still offer a holdings download to
every visitor. import_holdings_file() parses such a file into the common holdings
format (see etf_scraper.holdings), with source "manual_download".

    from etf_scraper import import_holdings_file
    df = import_holdings_file("WisdomTree", "fund-holdings-1000492.csv",
                              fund_isin="IE00...", as_of_date="2026-11-11")

No requests are made. Files often carry neither the fund's identity nor a date, so
pass fund_isin / fund_ticker and as_of_date; without as_of_date the file's
modification date (normally the download day) is used.
"""

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Union

import pandas as pd

from etf_scraper.holdings import normalize_holdings
from etf_scraper.parsers import read_table, to_number

logger = logging.getLogger(__name__)

SOURCE_MANUAL = "manual_download"


def _parse_wisdomtree(text: str) -> pd.DataFrame:
    """WisdomTree "fund-holdings-<id>.csv": Sector, Name, Weight ("5.86%")."""
    df = read_table(text)
    missing = {"Name", "Weight"} - set(df.columns)
    if missing:
        raise ValueError(f"Not a WisdomTree holdings file, missing columns {sorted(missing)}")
    df = df.rename(columns={"Sector": "sector", "Name": "name", "Weight": "weight"})
    df["weight"] = to_number(df["weight"]).values
    for col in ["sector", "name"]:
        if col in df:
            text_col = df[col].str.strip()
            df[col] = text_col.where(text_col != "", None)
    return df[df["name"].notna()].reset_index(drop=True)


PARSERS: Dict[str, Callable[[str], pd.DataFrame]] = {
    "WisdomTree": _parse_wisdomtree,
}


def import_holdings_file(
    provider: str,
    path: Union[str, Path],
    fund_isin: Optional[str] = None,
    fund_ticker: Optional[str] = None,
    as_of_date: Union[date, str, None] = None,
) -> pd.DataFrame:
    """Parse a holdings file downloaded by hand into the common holdings format.

    Args:
    - provider: one of PARSERS, eg "WisdomTree"
    - path: the downloaded file
    - fund_isin, fund_ticker: the fund the file belongs to (files often omit it)
    - as_of_date: the holdings date; defaults to the file's modification date
    """
    key = next((k for k in PARSERS if k.lower() == provider.lower()), None)
    if key is None:
        raise ValueError(f"No manual import for {provider!r}, available: {', '.join(PARSERS)}")
    path = Path(path).expanduser()
    df = PARSERS[key](path.read_text(encoding="utf-8-sig"))

    if as_of_date is None:
        as_of_date = datetime.fromtimestamp(path.stat().st_mtime).date()
        logger.info(f"{key}: no holdings date given, using the file date {as_of_date}")
    df["as_of_date"] = pd.Timestamp(as_of_date)
    return normalize_holdings(
        df,
        fund_ticker=fund_ticker,
        fund_isin=fund_isin.upper() if fund_isin else None,
        provider=key,
        source=SOURCE_MANUAL,
    )
