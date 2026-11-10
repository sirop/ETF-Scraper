"""iShares (BlackRock) European UCITS ETFs, via the iShares UK site.

Sources (all public, plain requests with an honest User-Agent):
- Holdings: each product page publishes a "Latest fund holdings CSV data" download
  (schema.org DataDownload) at <product page>/latest-holdings.csv
- Fund list: the UK product sitemap (https://www.ishares.com/uk/product-sitemap.xml)
  lists all product pages. Ticker, name and asset class come from each page's
  schema.org data, the ISIN from the page header. Only the first part of each page
  is downloaded.

Only the latest holdings are available.
"""

import json
import logging
import re
from datetime import date
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
from etf_scraper.parsers import parse_date, parse_dates, read_table, split_preamble, to_number
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)


class ISharesEUListings(ProviderListings):
    provider = Provider.IShares_EU.value
    session = ProviderSession(Provider.IShares_EU.value)

    site_root = "https://www.ishares.com/uk/individual/en/"
    sitemap_url = "https://www.ishares.com/uk/product-sitemap.xml"
    holdings_file = "latest-holdings.csv"
    # ticker, name, asset class and ISIN all sit near the top of the product page
    page_prefix_bytes = 250_000

    holding_col_mapping = {
        "Ticker": "ticker",
        "Issuer Ticker": "ticker",
        "Name": "name",
        "ISIN": "isin",
        "Sector": "sector",
        "Asset Class": "asset_class",
        "Market Value": "market_value",
        "Weight (%)": "weight",
        "Notional Value": "notional_value",
        "Shares": "amount",
        "Nominal": "amount",
        "Par Value": "par_value",
        "Price": "price",
        "Location": "location",
        "Exchange": "exchange",
        "Market Currency": "currency",
        "Duration": "duration",
        "Maturity": "maturity_date",
        "Coupon (%)": "coupon",
        "Effective Date": "effective_date",
    }
    numeric_cols = [
        "market_value",
        "weight",
        "notional_value",
        "amount",
        "par_value",
        "price",
        "duration",
        "coupon",
    ]
    date_cols = ["maturity_date", "effective_date"]
    required_cols = ["Name", "Market Value", "Weight (%)"]

    # ---------------------------------------------------------------- holdings

    @classmethod
    def holdings_url(cls, product_url: str) -> str:
        """The download link published on the product page. Use the exact product
        URL: the site's cache is per URL, so other spellings that also resolve (eg a
        different slug) can return an older file.
        """
        return product_url.rstrip("/") + "/" + cls.holdings_file

    @classmethod
    def _parse_holdings_resp(cls, resp_text: str):
        """Parse a latest-holdings.csv download. Returns (holdings_df, as_of_date)."""
        metadata, table = split_preamble(resp_text, cls.required_cols)

        as_of_str = next(
            (v for k, v in metadata.items() if "as of" in k.lower()), None
        )
        as_of_date = parse_date(as_of_str)
        if as_of_date is None:
            raise ValueError(f"Was expecting an 'as of' date, found metadata {metadata}")

        raw = read_table(table)
        unknown = [c for c in raw.columns if c not in cls.holding_col_mapping]
        if unknown:
            logger.debug(f"{cls.provider}: keeping unmapped columns {unknown}")

        holdings_df = raw.rename(columns=cls.holding_col_mapping)
        for col in cls.numeric_cols:
            if col in holdings_df:
                holdings_df[col] = to_number(holdings_df[col]).values
        for col in cls.date_cols:
            if col in holdings_df:
                holdings_df[col] = parse_dates(holdings_df[col]).values

        holdings_df["as_of_date"] = pd.Timestamp(as_of_date)
        return holdings_df, as_of_date

    @classmethod
    def retrieve_holdings(cls, ticker: str, product_url: str) -> pd.DataFrame:
        """Retrieve the latest holdings from the fund's product page download."""
        url = cls.holdings_url(product_url)
        logger.debug(f"Querying {cls.provider} for {ticker} holdings: {url}")
        resp = cls.session.get(url, expect=())
        holdings_df, _ = cls._parse_holdings_resp(resp.content.decode("utf-8-sig"))
        holdings_df["fund_ticker"] = ticker
        return holdings_df

    @classmethod
    def _retrieve_holdings(
        cls, sec_listing: SecurityListing, holdings_date: Union[date, None] = None
    ) -> pd.DataFrame:
        _check_exp_provider(sec_listing.provider, cls.provider, cls.__name__)
        if holdings_date:
            raise InvalidParameterError(
                f"{cls.provider} only provides the latest holdings (holdings_date=None)"
            )
        if not isinstance(sec_listing.product_url, str) or not sec_listing.product_url:
            raise InvalidParameterError(f"No product page known for {sec_listing.ticker}")
        return cls.retrieve_holdings(sec_listing.ticker, sec_listing.product_url)

    # ---------------------------------------------------------------- listings

    @classmethod
    def list_product_urls(cls) -> List[str]:
        """UCITS ETF product pages on the iShares UK site, from the product sitemap."""
        resp = cls.session.get(cls.sitemap_url, expect=())
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", resp.text)
        prefix = cls.site_root + "products/"
        urls = sorted({u for u in locs if u.startswith(prefix) and "ucits-etf" in u})
        return urls

    @staticmethod
    def _parse_product_page(page: str) -> dict:
        """Extract fund details from (the first part of) a product page."""
        info = {}
        for block in re.findall(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S
        ):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
            for node in nodes:
                types = node.get("@type") if isinstance(node, dict) else None
                types = types if isinstance(types, list) else [types]
                if "InvestmentFund" in types:
                    info["fund_name"] = node.get("name")
                    info["ticker"] = node.get("alternateName")
                    info["asset_class"] = node.get("category")
                    for ident in node.get("identifier") or []:
                        if ident.get("propertyID") == "ticker":
                            info["ticker"] = ident.get("value")

        for pattern in [
            r"\(ISIN:\s*([A-Z]{2}[A-Z0-9]{9}[0-9])\)",
            r"ISIN:\s*(?:&lt;strong&gt;|<strong>)?\s*([A-Z]{2}[A-Z0-9]{9}[0-9])",
            r'keyFundFacts-isin"[^>]*>\s*([A-Z]{2}[A-Z0-9]{9}[0-9])',
        ]:
            m = re.search(pattern, page)
            if m and is_valid_isin(m.group(1)):
                info["isin"] = m.group(1)
                break
        return info

    @classmethod
    def retrieve_listings(
        cls,
        max_funds: Optional[int] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> pd.DataFrame:
        """Build the iShares UCITS ETF listings.

        Downloads the start of every UCITS ETF product page (~900 pages at the
        session rate limit, so this takes a while). Pages that fail or miss a ticker
        are logged and skipped.

        Args:
        - max_funds: only process the first n product pages (for testing)
        - progress: optional callback(done, total, url)
        """
        urls = cls.list_product_urls()
        if max_funds:
            urls = urls[:max_funds]

        rows = []
        for i, url in enumerate(urls, 1):
            if progress:
                progress(i, len(urls), url)
            try:
                page = cls.session.get_prefix(url, cls.page_prefix_bytes)
            except Exception as e:
                logger.warning(f"{cls.provider}: skipping {url}: {type(e).__name__}: {e}")
                continue
            info = cls._parse_product_page(page)
            if not info.get("ticker"):
                logger.warning(f"{cls.provider}: no ticker found on {url}, skipping")
                continue
            info["product_url"] = url
            info["product_id"] = re.search(r"/products/(\d+)/", url).group(1)
            rows.append(info)

        listings = pd.DataFrame(
            rows,
            columns=["ticker", "fund_name", "isin", "asset_class", "product_url", "product_id"],
        )
        listings["fund_type"] = "ETF"
        listings["provider"] = cls.provider
        listings["domicile"] = listings["isin"].str[:2]
        listings["exchange"] = np.nan  # tickers are as shown on the UK site
        return listings
