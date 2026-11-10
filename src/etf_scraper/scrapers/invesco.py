"""Invesco US listings and holdings scraper."""

import logging
from typing import Union
from datetime import date

import pandas as pd
import numpy as np

from etf_scraper.utils import (
    check_missing_cols,
    check_data_mismatch,
    set_numeric_cols,
    strip_str_cols,
)
from etf_scraper.base import (
    Provider,
    ProviderListings,
    SecurityListing,
    InvalidParameterError,
)

from etf_scraper.http import ProviderSession
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)


class InvescoListings(ProviderListings):
    """Invesco ETFs via Invesco's public JSON API (dng-api.invesco.com).

    TODO: Currently only works for ETF data (ie we are missing Invesco MFs)
    """

    provider = Provider.Invesco.value

    api_host = "https://dng-api.invesco.com"
    search_endpoint = api_host + "/product/search"
    holdings_endpoint_fmt = (
        api_host + "/cache/v1/accounts/en_US/shareclasses/{cusip}/holdings/fund"
    )
    # Invesco's firewall rejects clients imitating a browser; ProviderSession
    # identifies the tool honestly
    session = ProviderSession(
        Provider.Invesco.value, headers={"Accept": "application/json"}
    )

    search_fields = [
        "ticker",
        "cusip",
        "title",
        "isin",
        "inceptionDate",
        "assetClass",
        "assetSubClass",
        "url",
    ]
    listings_resp_mapping = {
        "title": "fund_name",
        "ticker": "ticker",
        "inceptionDate": "inception_date",
        "cusip": "cusip",
        "isin": "isin",
        "assetClass": "asset_class",
        "assetSubClass": "subasset_class",
        "url": "product_url",
    }

    item_url = (
        "https://www.invesco.com/us/financial-products/etfs/"
        "product-detail?audienceType=Investor&ticker={}"
    )

    holdings_resp_mapping = {
        "ticker": "ticker",
        "cusip": "cusip",
        "issuerName": "name",
        "units": "amount",
        "marketValueBase": "market_value",
        "percentageOfTotalNetAssets": "weight",
        "securityTypeName": "security_type",
        "currency": "currency",
        "coupon": "coupon",
        "maturityDate": "maturity_date",
        "contractExpiryDate": "contract_expiry_date",
    }
    date_cols = ["maturity_date", "contract_expiry_date"]
    numeric_cols = ["amount", "market_value", "weight", "coupon"]

    @classmethod
    def _search_products(cls) -> pd.DataFrame:
        """Query Invesco's product catalog for all open US ETFs."""
        params = {
            "fq": [
                'countryCode:"US"',
                'language:"en_us"',
                'accountType:"ETF"',
                'contentType:"Product"',
                'shareClassStatus:"open"',
            ],
            "q": "_suggest_:*",
            "fl": ",".join(cls.search_fields),
            "rows": 1000,
            "start": 0,
        }
        resp = cls.session.get(cls.search_endpoint, params=params, expect=("json",))
        resp.raise_for_status()
        docs = resp.json()["response"]["docs"]

        # Solr may return multi valued fields as lists, keep the first value
        flat = [
            {k: (v[0] if isinstance(v, list) and v else v) for k, v in d.items()}
            for d in docs
        ]
        return pd.DataFrame(flat)

    @classmethod
    def retrieve_listings(cls) -> pd.DataFrame:
        """Retrieve all ETF listings from Invesco"""
        listings_df = cls._search_products()

        check_missing_cols(["ticker", "cusip"], listings_df.columns, raise_error=True)
        check_missing_cols(list(cls.listings_resp_mapping), listings_df.columns)

        listings_df_ = (
            listings_df.reindex(columns=list(cls.listings_resp_mapping))
            .rename(columns=cls.listings_resp_mapping)
            .dropna(subset=["ticker"])
        )
        listings_df_["inception_date"] = pd.to_datetime(
            listings_df_["inception_date"], errors="coerce"
        ).dt.date
        listings_df_["product_url"] = listings_df_["ticker"].apply(
            lambda x: cls.item_url.format(x)
        )
        listings_df_["fund_type"] = "ETF"
        listings_df_["provider"] = cls.provider

        return listings_df_.reset_index(drop=True)

    @classmethod
    def _resolve_cusip(cls, ticker: str) -> str:
        """Look up an Invesco ETF's CUSIP from its ticker via the product catalog."""
        products = cls._search_products()
        match = products[products["ticker"].str.upper() == ticker.upper()]
        if match.empty:
            raise InvalidParameterError(f"Invesco ETF not found for ticker {ticker}")
        return match["cusip"].iloc[0]

    @classmethod
    def _parse_holdings_resp(cls, holdings_resp: dict):
        """Parse the JSON Invesco's API returns for holdings data.

        Returns (holdings_df, as_of_date, cusip).
        """
        as_of_str = holdings_resp.get("effectiveDate") or holdings_resp.get(
            "effectiveBusinessDate"
        )
        if not as_of_str:
            raise ValueError("Invesco holdings response is missing an effective date")
        as_of_date = pd.Timestamp(as_of_str[:10])

        holdings_df = pd.DataFrame(holdings_resp.get("holdings") or [])
        if holdings_df.empty:
            raise InvalidParameterError("Invesco returned no holdings")

        check_missing_cols(
            ["issuerName", "units", "percentageOfTotalNetAssets"],
            holdings_df.columns,
            raise_error=True,
        )

        holdings_df_ = holdings_df.reindex(
            columns=list(cls.holdings_resp_mapping)
        ).rename(columns=cls.holdings_resp_mapping)

        for col in cls.date_cols:
            holdings_df_[col] = pd.to_datetime(holdings_df_[col], errors="coerce")

        strip_str_cols(holdings_df_, ["ticker", "cusip"])
        set_numeric_cols(holdings_df_, cls.numeric_cols)
        holdings_df_["as_of_date"] = as_of_date

        expected = holdings_resp.get("totalNumberOfHoldings")
        if expected is not None and int(expected) != len(holdings_df_):
            logger.warning(
                f"Invesco reported {expected} holdings but returned {len(holdings_df_)}"
            )

        return holdings_df_, as_of_date, holdings_resp.get("cusip")

    @classmethod
    def retrieve_holdings(cls, ticker: str, cusip: Union[str, None] = None):
        """Retrieve the latest holdings for the given ETF ticker.

        Invesco's API is keyed by CUSIP. If not given, it's looked up from the ticker.
        """
        if not cusip or (isinstance(cusip, float) and np.isnan(cusip)):
            cusip = cls._resolve_cusip(ticker)

        url = cls.holdings_endpoint_fmt.format(cusip=cusip)
        params = {"idType": "cusip", "productType": "ETF"}
        resp = cls.session.get(url, params=params, expect=("json",))
        resp.raise_for_status()

        holdings_df, _, ret_cusip = cls._parse_holdings_resp(resp.json())
        if ret_cusip:
            check_data_mismatch(cusip, ret_cusip, "cusip", raise_error=True)

        holdings_df["fund_ticker"] = ticker.upper()
        return holdings_df

    @classmethod
    def _retrieve_holdings(
        cls, sec_listing: SecurityListing, holdings_date: Union[None, date] = None
    ) -> pd.DataFrame:
        """Retrieve Invesco ETF holdings data"""
        _check_exp_provider(sec_listing.provider, cls.provider, cls.__name__)

        if holdings_date:
            raise InvalidParameterError(
                "Can only retrieve the latest holdings from Invesco (holdings_date=None). "
                "Use etf_scraper.edgar.query_nport_holdings for historical holdings."
            )

        return cls.retrieve_holdings(sec_listing.ticker, sec_listing.cusip)
