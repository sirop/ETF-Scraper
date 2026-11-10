"""Invesco European UCITS ETFs, via Invesco's public JSON API (dng-api.invesco.com).

Sources (plain requests with an honest User-Agent, the same API the fund pages use):
- Fund list: /product/search for the Irish site (all Invesco UCITS ETFs), which
  includes ticker, ISIN, Bloomberg ticker, asset type and domicile.
- Holdings: /cache/v1/accounts/en_IE/shareclasses/<ISIN>/holdings/fund?idType=isin
  The response has name, ISIN, weight and currency per holding, but no share
  counts or market values.

Synthetic (swap based) ETFs report their substitute basket as "fund" holdings, not
the index they track. Pass variation="index" to retrieve_holdings for the index
constituents instead.

Only the latest holdings are available.
"""

import logging
from datetime import date
from typing import Optional, Union

import numpy as np
import pandas as pd

from etf_scraper.base import (
    InvalidParameterError,
    Provider,
    ProviderListings,
    SecurityListing,
)
from etf_scraper.http import ProviderSession
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)

# Bloomberg exchange codes used for Invesco UCITS ETFs
BLOOMBERG_EXCHANGES = {
    "LN": "London Stock Exchange",
    "GY": "Xetra",
    "SW": "SIX Swiss Exchange",
    "IM": "Borsa Italiana",
    "NA": "Euronext Amsterdam",
    "FP": "Euronext Paris",
    "IX": "Cboe Europe",
}


class InvescoEUListings(ProviderListings):
    provider = Provider.Invesco_EU.value
    # Invesco's firewall rejects clients imitating a browser; ProviderSession
    # identifies the tool honestly
    session = ProviderSession(
        Provider.Invesco_EU.value, headers={"Accept": "application/json"}
    )

    api_host = "https://dng-api.invesco.com"
    site_host = "https://www.invesco.com"
    account = "en_IE"
    search_filters = [
        'countryCode:"IE"',
        'language:"en_ie"',
        'accountType:"ETF"',
        'contentType:"Product"',
        'shareClassStatus:"open"',
    ]
    search_fields = [
        "ticker",
        "isin",
        "title",
        "bloombergTicker",
        "assetType",
        "replicationMethod",
        "domicileCountry",
        "inceptionDate",
        "url",
    ]

    holdings_col_mapping = {
        "name": "name",
        "isin": "isin",
        "cusip": "cusip",
        "weight": "weight",
        "currency": "currency",
    }

    # ---------------------------------------------------------------- holdings

    @classmethod
    def holdings_url(cls, isin: str, variation: str = "fund") -> str:
        return (
            f"{cls.api_host}/cache/v1/accounts/{cls.account}/shareclasses/"
            f"{isin}/holdings/{variation}"
        )

    @classmethod
    def _parse_holdings_resp(cls, resp: dict):
        """Parse a holdings response. Returns (holdings_df, as_of_date)."""
        holdings = resp.get("holdings") or []
        as_of_str = resp.get("effectiveDate")
        if not holdings or not as_of_str:
            raise InvalidParameterError(
                f"{cls.provider}: no holdings published (eg a newly launched fund, or an "
                "exchange traded commodity holding physical gold or bitcoin)"
            )
        as_of_date = pd.Timestamp(as_of_str[:10])

        holdings_df = pd.DataFrame(holdings).rename(columns=cls.holdings_col_mapping)
        holdings_df["weight"] = pd.to_numeric(holdings_df.get("weight"), errors="coerce")

        expected = resp.get("numberOfHoldings")
        if expected is not None and int(expected) not in (len(holdings_df), len(holdings_df) - 1):
            # not a reliable check: the list can include a cash line, and for
            # synthetic ETFs numberOfHoldings counts the index, not the basket
            logger.debug(
                f"{cls.provider}: reported {expected} holdings, returned {len(holdings_df)}"
            )

        holdings_df["as_of_date"] = as_of_date
        return holdings_df, as_of_date.date()

    @classmethod
    def retrieve_holdings(
        cls, ticker: str, isin: str, variation: str = "fund"
    ) -> pd.DataFrame:
        """Retrieve the latest holdings for a UCITS ETF share class.

        Args:
        - variation: "fund" (the fund's holdings, the substitute basket for synthetic
        ETFs) or "index" (the tracked index's constituents)
        """
        if variation not in ("fund", "index"):
            raise ValueError(f"variation must be 'fund' or 'index', not {variation!r}")
        url = cls.holdings_url(isin, variation)
        logger.debug(f"Querying {cls.provider} for {ticker} {variation} holdings: {url}")
        resp = cls.session.get(url, params={"idType": "isin"}, expect=("json",))
        holdings_df, _ = cls._parse_holdings_resp(resp.json())
        holdings_df["fund_ticker"] = ticker
        holdings_df["holdings_variation"] = variation
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
        if not isinstance(sec_listing.isin, str) or not sec_listing.isin:
            raise InvalidParameterError(f"No ISIN known for {sec_listing.ticker}")
        return cls.retrieve_holdings(sec_listing.ticker, sec_listing.isin)

    # ---------------------------------------------------------------- listings

    @classmethod
    def _search_products(cls) -> pd.DataFrame:
        params = {
            "fq": cls.search_filters,
            "q": "_suggest_:*",
            "fl": ",".join(cls.search_fields),
            "rows": 1000,
            "start": 0,
        }
        resp = cls.session.get(
            cls.api_host + "/product/search", params=params, expect=("json",)
        )
        docs = resp.json()["response"]["docs"]
        # Solr may return multi valued fields as lists, keep the first value
        flat = [
            {k: (v[0] if isinstance(v, list) and v else v) for k, v in d.items()}
            for d in docs
        ]
        return pd.DataFrame(flat, columns=cls.search_fields)

    @classmethod
    def retrieve_listings(cls, max_funds: Optional[int] = None) -> pd.DataFrame:
        """All open Invesco UCITS ETF share classes (one request)."""
        products = cls._search_products()
        if max_funds:
            products = products.head(max_funds)

        bbg = products["bloombergTicker"].fillna("")
        listings = pd.DataFrame(
            {
                "ticker": products["ticker"],
                "fund_name": products["title"],
                "isin": products["isin"],
                "product_id": bbg.replace("", np.nan),
                "exchange": bbg.str.split().str[-1].map(BLOOMBERG_EXCHANGES),
                "asset_class": products["assetType"],
                "domicile": products["domicileCountry"],
                "inception_date": pd.to_datetime(
                    products["inceptionDate"], errors="coerce"
                ).dt.date,
                "product_url": cls.site_host
                + products["url"].fillna("").str.replace("/content/invesco", "", regex=False),
            }
        )
        listings["fund_type"] = "ETF"
        listings["provider"] = cls.provider
        return listings.dropna(subset=["ticker", "isin"]).reset_index(drop=True)
