"""Vanguard US listings and holdings scraper."""

import logging
from typing import Union
from datetime import date, datetime

import requests
import pandas as pd
import numpy as np

from etf_scraper.utils import (
    check_missing_cols,
    strip_str_cols,
)
from etf_scraper.base import (
    Provider,
    ProviderListings,
    SecurityListing,
    InvalidParameterError,
)

from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)


class VanguardListings(ProviderListings):
    provider = Provider.Vanguard.value
    req_user_header = {
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
    }  # need to pass otherwise requests will be denied
    listing_endpoint = (
        "https://investor.vanguard.com/investment-products/list/funddetail"
    )
    listing_resp_mapping = {
        "ticker": "ticker",
        "cusip": "cusip",
        "longName": "fund_name",
        "inceptionDate": "inception_date",
        "fundId": "product_id",
        "style": "asset_class",
        "type": "subasset_class",
    }
    fund_url = "https://advisors.vanguard.com/investments/products/{}"

    # Vanguard splits holdings by category, each served from its own endpoint.
    # A fund returns an empty list for the categories it doesn't hold.
    holding_categories = ["stock", "bond", "short-term-reserve"]
    holdings_page_size = 20000

    holding_col_mapping = {
        "asOfDate": "as_of_date",
        "ticker": "ticker",
        "sedol": "sedol",
        "cusip": "cusip",
        "isin": "isin",
        "longName": "name",
        "sharesHeld": "amount",
        "percentWeight": "weight",
        "secMainType": "security_type",
        "secSubType": "security_subtype",
        "holdingType": "holding_type",
        "marketValue": "market_value",
        "notionalValue": "notional_value",
        "faceAmount": "face_amount",
        "couponRate": "coupon",
        "maturityDate": "maturity_date",
    }
    numeric_cols = [
        "amount",
        "weight",
        "market_value",
        "notional_value",
        "face_amount",
        "coupon",
    ]

    @classmethod
    def holdings_endpoint(cls, ticker: str, category: str = "stock") -> str:
        """Endpoint to send a GET request to for querying holdings of one category
        (stock/bond/short-term-reserve).

        Note: as of Nov 2026 this only serves the latest month end holdings. Date
        parameters are dropped by Vanguard's proxy, so historical holdings must come
        from SEC N-PORT filings instead (see etf_scraper.edgar).
        """
        return (
            "https://investor.vanguard.com/vmf/api/"
            f"{ticker.upper()}/portfolio-holding/{category}.json"
        )

    @classmethod
    def retrieve_listings(cls):
        """
        #TODO: missing important info: eg AUM, country etf - not possible to get without visiting individual
        fund pages
        #FIXME: duplicates via multiple share classes, eg VFIAX, VFFSX, VOO
        #FIXME: as of Nov 2026 the listing endpoint returns HTML instead of JSON.
        """
        resp = requests.get(cls.listing_endpoint, headers=cls.req_user_header)
        resp.raise_for_status()
        fund_data = resp.json()
        fund_list = [x["profile"] for x in fund_data["fund"]["entity"]]
        fund_df = pd.DataFrame(fund_list)

        check_missing_cols(["ticker"], fund_df.columns, raise_error=True)
        check_missing_cols(list(cls.listing_resp_mapping), fund_df.columns)

        fund_df_ = fund_df.dropna(subset="ticker").rename(
            columns=cls.listing_resp_mapping
        )

        def map_fund_type(fund_row: pd.Series):
            if fund_row["isETF"]:
                return "ETF"
            elif fund_row["isMutualFund"]:
                return "MF"
            return np.nan

        fund_types = fund_df_.apply(map_fund_type, axis=1)

        fund_df_ = fund_df_.reindex(columns=list(cls.listing_resp_mapping.values()))

        fund_df_["fund_type"] = fund_types

        map_to_url = lambda x: cls.fund_url.format(x.upper())
        fund_df_["product_url"] = fund_df_["ticker"].apply(map_to_url)

        def parse_inception_date(x):
            try:
                return datetime.fromisoformat(x).date()
            except:
                return pd.NaT

        fund_df_["inception_date"] = fund_df_["inception_date"].apply(
            parse_inception_date
        )
        fund_df_["provider"] = cls.provider

        return fund_df_

    @staticmethod
    def _parse_vanguard_date(date_str) -> Union[pd.Timestamp, None]:
        """Vanguard dates look like '2026-09-30T00:00:00-04:00'. Keep the local date only."""
        if not isinstance(date_str, str) or not date_str:
            return pd.NaT
        return pd.Timestamp(date_str[:10])

    @classmethod
    def _parse_holdings_resp(cls, holdings_resp: dict, category: str = "stock"):
        """Parse one response from the Vanguard holdings API.

        Returns (holdings_df, as_of_date). holdings_df is empty if the fund has no
        holdings in this category.
        """
        as_of_date = cls._parse_vanguard_date(holdings_resp.get("asOfDate"))
        entities = (holdings_resp.get("fund") or {}).get("entity") or []

        holdings_df_raw = pd.DataFrame(entities)
        if holdings_df_raw.empty:
            return pd.DataFrame(columns=list(cls.holding_col_mapping.values())), as_of_date

        check_missing_cols(
            ["asOfDate", "longName", "sharesHeld", "marketValue", "percentWeight"],
            holdings_df_raw.columns,
            raise_error=True,
        )

        holdings_df = holdings_df_raw.reindex(
            columns=list(cls.holding_col_mapping)
        ).rename(columns=cls.holding_col_mapping)

        holdings_df["as_of_date"] = holdings_df["as_of_date"].apply(
            cls._parse_vanguard_date
        )
        holdings_df["as_of_date"] = pd.to_datetime(holdings_df["as_of_date"])
        holdings_df["maturity_date"] = pd.to_datetime(
            holdings_df["maturity_date"].apply(cls._parse_vanguard_date)
        )
        holdings_df["holding_category"] = category

        # Vanguard sends blanks rather than nulls for missing identifiers
        id_cols = ["ticker", "sedol", "cusip", "isin", "security_type", "security_subtype", "holding_type"]
        strip_str_cols(holdings_df, id_cols)
        for col in id_cols:
            holdings_df[col] = holdings_df[col].where(holdings_df[col] != "", np.nan)
        # Vanguard sometimes sends placeholders like "?" (eg faceAmount for BND), so
        # treat anything unparseable as missing rather than failing the whole fund
        for col in cls.numeric_cols:
            holdings_df[col] = pd.to_numeric(holdings_df[col], errors="coerce")

        return holdings_df, as_of_date

    @classmethod
    def _query_holdings_category(cls, ticker: str, category: str):
        """Query all pages of one holdings category for the given ticker."""
        url = cls.holdings_endpoint(ticker, category)
        frames, as_of_date, start = [], None, 1

        while True:
            params = {"start": start, "count": cls.holdings_page_size}
            resp = requests.get(url, params=params)
            resp.raise_for_status()
            resp_data = resp.json()

            page_df, page_date = cls._parse_holdings_resp(resp_data, category)
            as_of_date = page_date if as_of_date is None else as_of_date
            if not page_df.empty:
                frames.append(page_df)

            total = int(resp_data.get("size") or 0)
            start += cls.holdings_page_size
            if page_df.empty or start > total:
                break

        holdings_df = pd.concat(frames, ignore_index=True) if frames else None
        return holdings_df, as_of_date

    @classmethod
    def retrieve_holdings(
        cls, fund_ticker: str, holdings_date: Union[date, None] = None
    ) -> pd.DataFrame:
        """Retrieve the latest month end holdings for a Vanguard ETF/MF.

        Args:
        - fund_ticker: ETF/MF ticker, eg VOO
        - holdings_date: if given, must equal the latest month end Vanguard publishes,
        otherwise raises InvalidParameterError. For older dates use etf_scraper.edgar.

        Note: ticker may be missing, eg for short-term reserve positions
        """
        frames, as_of_dates = [], set()
        for category in cls.holding_categories:
            logger.debug(f"Querying Vanguard {category} holdings for {fund_ticker}")
            cat_df, cat_date = cls._query_holdings_category(fund_ticker, category)
            if cat_df is not None:
                frames.append(cat_df)
                as_of_dates.add(cat_date)

        if not frames:
            raise InvalidParameterError(
                f"No Vanguard holdings returned for ticker: {fund_ticker}"
            )
        if len(as_of_dates) > 1:
            raise ValueError(
                f"Vanguard returned multiple holdings dates for {fund_ticker}: {sorted(as_of_dates)}"
            )

        ret_holdings_date = as_of_dates.pop().date()
        if holdings_date and holdings_date != ret_holdings_date:
            raise InvalidParameterError(
                f"Vanguard only provides the latest month end holdings ({ret_holdings_date}), "
                f"not {holdings_date}. Use etf_scraper.edgar.query_nport_holdings for "
                "historical holdings."
            )

        holdings_df = pd.concat(frames, ignore_index=True)
        holdings_df["fund_ticker"] = fund_ticker
        return holdings_df

    @classmethod
    def _retrieve_holdings(
        cls, sec_listing: SecurityListing, holdings_date: Union[date, None] = None
    ) -> pd.DataFrame:
        """Retrieve Vanguard ETF/MF holdings.

        Note: as of writing, Vanguard only provides the latest month end data.
        """
        _check_exp_provider(sec_listing.provider, cls.provider, cls.__name__)
        return cls.retrieve_holdings(sec_listing.ticker, holdings_date)
