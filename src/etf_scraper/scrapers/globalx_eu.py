"""Global X Europe UCITS ETFs (globalxetfs.eu).

Sources (public, plain requests with an honest User-Agent; robots.txt allows all):
- Holdings: CSV downloads offered on each fund page,
  /api/funds/<fund>/topholdingscsv (despite the name, all holdings of physically
  replicated funds) or, when that is empty, /api/funds/<fund>/basketconstituentscsv
  (the substitute basket of synthetic funds, eg QYLD).
- Fund list: the site's sitemap lists the fund pages (/funds/<fund>); each page
  gives the fund name, primary (accumulating) ISIN and distributing ISIN, if any.

Only the latest holdings are available.
"""

import html
import logging
import re
from datetime import date
from io import StringIO
from typing import Callable, List, Optional, Union

import numpy as np
import pandas as pd

from etf_scraper.base import (
    InvalidParameterError,
    Provider,
    ProviderListings,
    SecurityListing,
)
from etf_scraper.http import ProviderSession
from etf_scraper.identifiers import is_valid_isin
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)


class GlobalXEUListings(ProviderListings):
    provider = Provider.GlobalX_EU.value
    session = ProviderSession(Provider.GlobalX_EU.value)

    host = "https://globalxetfs.eu"
    sitemap_url = "https://globalxetfs.eu/sitemap.xml"
    holdings_sources = ["topholdingscsv", "basketconstituentscsv"]

    holding_col_mapping = {
        "AS_OF_DATE": "as_of_date",
        "NET_ASSETS": "weight",
        "TICKER": "ticker",
        "NAME": "name",
        "SEDOL": "sedol",
        "ISIN": "isin",
        "COUNTRY": "location",
        "MARKET_PRICE": "price",
        "SHARES_HELD": "amount",
        "MARKET_VALUE": "market_value",
    }
    numeric_cols = ["weight", "price", "amount", "market_value"]

    # ---------------------------------------------------------------- holdings

    @classmethod
    def holdings_url(cls, fund_slug: str, source: str = "topholdingscsv") -> str:
        return f"{cls.host}/api/funds/{fund_slug.lower()}/{source}"

    @classmethod
    def _parse_holdings_resp(cls, text: str) -> pd.DataFrame:
        """Parse a holdings CSV. Returns an empty dataframe if the file has no rows."""
        text = text.lstrip("﻿")
        if not text.strip():
            return pd.DataFrame()

        first_line = text.strip().splitlines()[0]
        if "AS_OF_DATE" not in first_line:
            # funds without this file return only the disclaimer line
            if first_line.startswith(("The information", '"The information')):
                return pd.DataFrame()
            raise ValueError(f"Unexpected holdings CSV header: {first_line[:200]}")
        raw = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False)

        # keep holding rows only; a disclaimer follows the table
        raw = raw[raw["AS_OF_DATE"].str.match(r"^\d{4}-\d{2}-\d{2}$")]
        holdings_df = raw.rename(columns=cls.holding_col_mapping).reset_index(drop=True)
        holdings_df = holdings_df.replace({"": np.nan})

        for col in cls.numeric_cols:
            if col in holdings_df:
                holdings_df[col] = pd.to_numeric(holdings_df[col], errors="coerce")

        # basket files put 9 character CUSIPs in the SEDOL column (SEDOLs have 7)
        if "sedol" in holdings_df:
            is_cusip = holdings_df["sedol"].str.len() == 9
            holdings_df["cusip"] = holdings_df["sedol"].where(is_cusip)
            holdings_df["sedol"] = holdings_df["sedol"].where(~is_cusip)

        holdings_df["as_of_date"] = pd.to_datetime(holdings_df["as_of_date"])
        return holdings_df

    @classmethod
    def retrieve_holdings(cls, ticker: str, fund_slug: str) -> pd.DataFrame:
        """Retrieve the latest holdings, trying the holdings file first and the
        basket file (synthetic funds) second."""
        for source in cls.holdings_sources:
            url = cls.holdings_url(fund_slug, source)
            logger.debug(f"Querying {cls.provider} for {ticker} holdings: {url}")
            resp = cls.session.get(url, expect=("csv", "text"))
            holdings_df = cls._parse_holdings_resp(resp.content.decode("utf-8-sig"))
            if not holdings_df.empty:
                holdings_df["fund_ticker"] = ticker
                holdings_df["holdings_source"] = source
                return holdings_df
        raise InvalidParameterError(f"{cls.provider}: no holdings published for {ticker}")

    @classmethod
    def _retrieve_holdings(
        cls, sec_listing: SecurityListing, holdings_date: Union[date, None] = None
    ) -> pd.DataFrame:
        _check_exp_provider(sec_listing.provider, cls.provider, cls.__name__)
        if holdings_date:
            raise InvalidParameterError(
                f"{cls.provider} only provides the latest holdings (holdings_date=None)"
            )
        slug = sec_listing.product_id
        if not isinstance(slug, str) or not slug:
            raise InvalidParameterError(f"No fund page id (product_id) for {sec_listing.ticker}")
        return cls.retrieve_holdings(sec_listing.ticker, slug)

    # ---------------------------------------------------------------- listings

    @classmethod
    def list_fund_slugs(cls) -> List[str]:
        """Fund page ids (eg 'botz') from the sitemap, English pages only."""
        text = cls.session.get(cls.sitemap_url, expect=()).text
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text)
        return sorted({m.group(1) for loc in locs for m in [re.match(rf"{cls.host}/funds/([\w-]+)/?$", loc)] if m})

    @staticmethod
    def _parse_fund_page(page: str) -> dict:
        """Fund name, primary and distributing ISIN (and its ticker) from a fund page."""
        info = {}
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        if title:
            info["fund_name"] = html.unescape(re.sub(r"\s*\|.*$", "", title.group(1)).strip())
        m = re.search(r"Primary ISIN</div>\s*<div[^>]*>\s*([A-Z]{2}[A-Z0-9]{9}\d)\s*<", page)
        if m and is_valid_isin(m.group(1)):
            info["isin"] = m.group(1)
        page_u = page.replace('\\"', '"')
        m = re.search(r'"distIsin":"([A-Z]{2}[A-Z0-9]{9}\d)"', page_u)
        if m and is_valid_isin(m.group(1)):
            info["dist_isin"] = m.group(1)
            dist_block = page_u[m.end(): m.end() + 2000]
            t = re.search(r'"ticker":"([A-Z0-9]+)(?: [A-Z]{2})?"', dist_block)
            if t:
                info["dist_ticker"] = t.group(1)
        m = re.search(r'"domicile":"([A-Za-z ]+)"', page_u)
        if m and m.group(1) != "Domicile":
            info["domicile_name"] = m.group(1)
        return info

    @classmethod
    def retrieve_listings(
        cls,
        max_funds: Optional[int] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> pd.DataFrame:
        """One row per share class (primary and distributing) of every fund."""
        slugs = cls.list_fund_slugs()
        if max_funds:
            slugs = slugs[:max_funds]

        rows = []
        for i, slug in enumerate(slugs, 1):
            url = f"{cls.host}/funds/{slug}"
            if progress:
                progress(i, len(slugs), url)
            try:
                info = cls._parse_fund_page(cls.session.get(url, expect=("html",)).text)
            except Exception as e:
                logger.warning(f"{cls.provider}: skipping {url}: {type(e).__name__}: {e}")
                continue
            base = {"fund_name": info.get("fund_name"), "product_id": slug, "product_url": url}
            if info.get("isin"):
                rows.append({**base, "ticker": slug.upper(), "isin": info["isin"]})
            else:
                logger.warning(f"{cls.provider}: no primary ISIN on {url}")
            # income funds have a distributing primary class: same ISIN, no extra row
            if (
                info.get("dist_isin")
                and info.get("dist_ticker")
                and info["dist_isin"] != info.get("isin")
            ):
                rows.append({**base, "ticker": info["dist_ticker"], "isin": info["dist_isin"]})

        listings = pd.DataFrame(rows, columns=["ticker", "fund_name", "isin", "product_id", "product_url"])
        listings["domicile"] = listings["isin"].str[:2]
        # crypto ETPs (exchange traded notes, eg "Aave ETP") hold no securities
        is_etp = listings["fund_name"].fillna("").str.contains(r"\bETP\b")
        listings["fund_type"] = np.where(is_etp, "ETP", "ETF")
        listings["provider"] = cls.provider
        return listings
