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

import html
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Union

import pandas as pd

from etf_scraper.config import WISDOMTREE_FUNDS_FILE
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

# ---------------------------------------------------------------- WisdomTree ids

WT_FILE_ID_RE = re.compile(r"fund-holdings-(\d+)")


def load_wisdomtree_funds() -> pd.DataFrame:
    """WisdomTree product numbers (as in "fund-holdings-<number>.csv") with ISIN,
    ticker and name of the share class, from data/wisdomtree_funds.csv."""
    return pd.read_csv(WISDOMTREE_FUNDS_FILE, dtype=str, keep_default_na=False)


def wisdomtree_funds_from_page(page: str) -> pd.DataFrame:
    """Product number, ISIN, ticker and name pairs found in a WisdomTree fund page
    saved in a browser (used by scripts/update_wisdomtree_funds.py).

    Two sources on the page: embedded product data ("productId":"1000492.product",
    "isin":..., "productName":..., "ticker":...), and the page's own share class
    (the header's ticker, name and ISIN, with the product number its download links
    use).
    """
    text = page.replace('\\"', '"')
    rows = {}
    for m in re.finditer(
        r'"productId":"(\d+)\.product","isin":"([A-Z]{2}[A-Z0-9]{9}[0-9])"'
        r'(?:,"[^"]+":"[^"]*")*?,"productName":"([^"]+)","ticker":"([A-Z0-9]+)"',
        text,
    ):
        rows[m[1]] = {"fund_id": m[1], "isin": m[2], "ticker": m[4], "fund_name": m[3]}

    header = re.search(
        r'tabindex="0">([A-Z0-9]{2,8})</span><span class="typ-h3">([^<]+)</span></h1>'
        r'.{0,400}?ISIN: ([A-Z]{2}[A-Z0-9]{9}[0-9])',
        text,
        re.S,
    )
    link_ids = re.findall(r"(\d{6,8})\.product/[^/\"]+/download", text)
    candidates = [i for i in link_ids if i not in rows]
    if header and candidates:
        page_id = max(set(candidates), key=candidates.count)
        rows[page_id] = {
            "fund_id": page_id, "isin": header[3], "ticker": header[1],
            "fund_name": html.unescape(header[2]).strip(),
        }
    return pd.DataFrame(list(rows.values()), columns=["fund_id", "isin", "ticker", "fund_name"])


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

    if key == "WisdomTree" and not (fund_isin or fund_ticker):
        m = WT_FILE_ID_RE.search(path.name)
        known = load_wisdomtree_funds()
        match = known[known["fund_id"] == m[1]] if m else known.iloc[0:0]
        if len(match):
            fund_isin, fund_ticker = match.iloc[0]["isin"], match.iloc[0]["ticker"]
            df["fund_name"] = match.iloc[0]["fund_name"]
        else:
            logger.warning(
                f"{key}: unknown fund {path.name}; pass fund_isin, or add it with "
                "scripts/update_wisdomtree_funds.py"
            )

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
