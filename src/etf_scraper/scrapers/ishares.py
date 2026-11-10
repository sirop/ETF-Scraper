"""iShares (BlackRock) US listings and holdings scraper."""

import logging
from typing import Union, List
from urllib.parse import urljoin
from io import StringIO
from datetime import date, datetime

import requests
import pandas as pd
import numpy as np

from etf_scraper.utils import (
    check_missing_cols,
    check_data_mismatch,
    safe_urljoin,
    set_numeric_cols,
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


class ISharesListings(ProviderListings):
    provider = Provider.IShares.value
    host = "https://www.ishares.com"
    listing_endpoint = (
        "/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/product-screener-v3/"
        "data/en/us-ishares/ishares-product-screener-backend-config&siteEntryPassthrough=true"
    )
    ajax_endpoint = "1467271812596.ajax"

    listing_resp_mapping = {
        "fundName": "fund_name",
        "inceptionDate": "inception_date",
        "localExchangeTicker": "ticker",
        "cusip": "cusip",
        "isin": "isin",
        "aladdinAssetClass": "asset_class",
        "aladdinSubAssetClass": "subasset_class",
        "aladdinCountry": "country",
        "aladdinRegion": "region",
        "productPageUrl": "product_url",
        "portfolioId": "product_id",
        "totalNetAssets": "net_assets",
        "productView": "fund_type",
    }
    exp_cols = ["productPageUrl", "localExchangeTicker"]  # bare minimum to be returned

    holding_col_mapping = {
        "Ticker": "ticker",  # for equity funds
        "Name": "name",
        "Sector": "sector",
        "Asset Class": "asset_class",
        "Market Value": "market_value",
        "Weight (%)": "weight",
        "Notional Value": "notional_value",
        "Quantity": "amount",
        "Price": "price",
        "Location": "location",
        "Exchange": "exchange",
        "Currency": "currency",
        "FX Rate": "fx_rate",
        "Market Currency": "market_currency",
        "Par Value": "par_value",  # for bond funds
        "CUSIP": "cusip",
        "ISIN": "isin",
        "SEDOL": "sedol",
        "Duration": "duration",
        "YTM (%)": "yield_to_maturity",
        "Maturity": "maturity",
        "Coupon (%)": "coupon",
        "Mod. Duration": "modified_duration",
        "Yield to Call (%)": "yield_to_call",
        "Yield to Worst (%)": "yield_to_worst",
        "Real Duration": "real_duration",
        "Real YTM (%)": "real_ytm",
        "Accrual Date": "accrual_date",
        "Effective Date": "effective_date",
    }

    holdings_string_cols = ["ticker", "cusip", "isin", "sedol"]
    holdings_numeric_cols = [
        "amount",
        "weight",
        "market_value",
        "price",
        "notional_value",
        "duration",
        "yield_to_maturity",
        "coupon",
        "modified_duration",
        "yield_to_call",
        "yield_to_worst",
        "real_duration",
        "real_ytm",
    ]
    holdings_date_cols = ["accrual_date", "effective_date"]

    _fund_type_map = {"etf": "ETF", "mutualfund": "MF"}

    @classmethod
    def exp_holding_cols(cls, asset_class: str) -> List[str]:
        if asset_class in ("Equity", "Real Estate", "Commodity"):
            return ["Ticker", "Quantity", "Market Value"]
        elif asset_class == "Fixed Income":
            return ["CUSIP", "Market Value"]
        else:
            raise NotImplementedError(
                f"iShares holdings scraper not implemented for {asset_class}"
            )

    @classmethod
    def retrieve_listings(cls):
        listing_url = urljoin(cls.host, cls.listing_endpoint)
        resp = requests.get(listing_url)
        resp.raise_for_status()

        resp_df = pd.DataFrame(resp.json())

        check_missing_cols(cls.listing_resp_mapping, resp_df.index)
        check_missing_cols(cls.exp_cols, resp_df.index, raise_error=True)

        resp_df_ = (
            resp_df.reindex(cls.listing_resp_mapping)
            .rename(index=cls.listing_resp_mapping)
            .T
        )

        build_url = lambda x: safe_urljoin(cls.host, x)
        resp_df_.loc[:, "product_url"] = resp_df_["product_url"].apply(build_url)

        def parse_date(x):
            if date_ := x.get("r", None):
                try:
                    return datetime.strptime(str(date_), "%Y%m%d").date()
                except:
                    return pd.NaT
            return pd.NaT

        def parse_net(x):
            try:
                return float(x.get("r"))
            except:
                return np.nan

        parse_fund_type = lambda x: cls._fund_type_map.get(x[0].lower(), np.nan)

        resp_df_.loc[:, "inception_date"] = resp_df_["inception_date"].apply(parse_date)
        resp_df_.loc[:, "net_assets"] = resp_df_["net_assets"].apply(parse_net)
        resp_df_.loc[:, "fund_type"] = resp_df_["fund_type"].apply(parse_fund_type)
        resp_df_.loc[:, "provider"] = cls.provider
        return resp_df_.reset_index(drop=True)

    @classmethod
    def _parse_holdings_date(cls, date_str: str) -> date:
        return datetime.strptime(date_str, "%b %d, %Y").date()  # eg "Jan 03, 2022"

    @classmethod
    def _parse_holdings_resp(cls, resp_content, asset_class="Equity"):
        header_rows = 9
        raw_data = StringIO(resp_content)
        summary_data = [raw_data.readline().rstrip("\n") for _ in range(header_rows)]

        as_of_date = {
            k.split(",", 1)[-1].strip("'\"")
            for k in summary_data
            if "Fund Holdings as of".lower() in k.lower()
        }

        if len(as_of_date) != 1:
            raise ValueError(
                f"Was expecting an 'as of date' indicator, instead found: {as_of_date}"
            )

        as_of_date = list(as_of_date)[0]

        if as_of_date == "-":
            raise InvalidParameterError(
                f"Found '-' as holdings date -> no data returned"
            )

        logger.debug(f"Found reported holdings date string {as_of_date}")
        logger.debug("Attempting to parse holdings data")

        as_of_date = cls._parse_holdings_date(as_of_date)

        if summary_data[-1] != "\xa0":
            logger.warning(
                f"Was expecting \xa0 as the last line in the summary block."
                f" Found {summary_data[-1]} instead."
            )

        holdings_df = pd.read_csv(
            raw_data, thousands=",", na_values="-"
        )  # shouldn't need to skip any rows now

        # BUG: sometimes the content is duplicated, so drop the duplicated information
        doc_intro = summary_data[0]
        dupl_ind = holdings_df.iloc[:, 0].fillna("").str.strip() == doc_intro.strip()

        if n := dupl_ind.sum():
            logger.warning(
                f"Detected response contains {n} duplicates, attempting to remove them"
            )
            cutoff = dupl_ind.replace(False, np.nan).first_valid_index()
            holdings_df = pd.read_csv(
                StringIO(resp_content),
                skiprows=header_rows,
                nrows=cutoff,
                thousands=",",
                na_values="-",
            )

        check_missing_cols(
            cls.exp_holding_cols(asset_class), holdings_df.columns, raise_error=True
        )
        holdings_df = holdings_df.rename(columns=cls.holding_col_mapping)

        def _parse_holdings_date_(x):
            try:
                return cls._parse_holdings_date(x)
            except:
                return pd.NaT

        for col in holdings_df:
            if col in cls.holdings_date_cols:
                parsed_dates = holdings_df[col].apply(_parse_holdings_date_)
                holdings_df[col] = pd.to_datetime(parsed_dates)
            elif col in cls.holdings_string_cols:
                holdings_df.loc[:, col] = holdings_df[col].astype(str)

        holdings_df = holdings_df[~holdings_df["weight"].isna()]

        strip_str_cols(
            holdings_df, [k for k in cls.holdings_string_cols if k in holdings_df]
        )
        set_numeric_cols(
            holdings_df, [k for k in cls.holdings_numeric_cols if k in holdings_df]
        )
        return holdings_df, as_of_date

    @classmethod
    def retrieve_holdings(
        cls,
        ticker: str,
        product_url: str,
        holdings_date: Union[date, None],
        asset_class: str = "Equity",
    ):
        """Query for IShares product holdings
        Args:
        - holdings_date: date for holdings to query. If not given, then will
        query for the latest available holdings.
        """
        endpoint = safe_urljoin(product_url, cls.ajax_endpoint)
        req_params = {
            "fileType": "csv",
            "fileName": f"{ticker}_holdings",
            "dataType": "fund",
        }
        if holdings_date:
            req_params = {"asOfDate": holdings_date.strftime("%Y%m%d"), **req_params}

        logger.debug(
            f"Querying {cls.provider} for {ticker} holdings as of {holdings_date}"
        )
        resp = requests.get(endpoint, params=req_params)
        resp.raise_for_status()

        holdings_df, as_of_date = cls._parse_holdings_resp(
            resp.content.decode(encoding="UTF-8-SIG"), asset_class
        )

        if holdings_date:
            check_data_mismatch(
                holdings_date, as_of_date, "holdings date", raise_error=True
            )

        holdings_df.loc[:, "fund_ticker"] = ticker
        holdings_df.loc[:, "as_of_date"] = pd.to_datetime(as_of_date)
        return holdings_df

    @classmethod
    def _retrieve_holdings(cls, sec_listing: SecurityListing, holdings_date: date):
        _check_exp_provider(sec_listing.provider, cls.provider, cls.__name__)
        return cls.retrieve_holdings(
            sec_listing.ticker,
            sec_listing.product_url,
            holdings_date,
            sec_listing.asset_class,
        )
