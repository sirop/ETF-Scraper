"""SSGA (SPDR) US listings and holdings scraper."""

import logging
from typing import Union
from io import BytesIO
from datetime import date, datetime

import requests
import pandas as pd
import numpy as np

from etf_scraper.utils import (
    check_missing_cols,
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


class SSGAListings(ProviderListings):
    provider = Provider.SSGA.value
    host = "https://www.ssga.com"

    # FIXME: values should be enum
    ssga_products = {"mf": "MF", "etfs": "ETF"}  # ignore 'cash' and 'strategies'

    ssga_web_url = "https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=us&language=en&role=intermediary&product=@all&ui=fund-finder"

    ssga_web_resp_renaming = {
        "fundName": "fund_name",
        "fundTicker": "ticker",
        "fundUri": "product_url",
        "inceptionDate": "inception_date",
        "domicile": "region",
        "aum": "net_assets",
        "primaryExchange": "exchange",
    }
    exp_web_cols = ["fundTicker", "fundUri"]

    ssga_doc_url = "https://www.ssga.com/us/en/intermediary/ic/library-content/products/fund-data/etfs/us/spdr-product-data-us-en.xlsx"
    ssga_doc_mapping = {
        "Ticker": "ticker",
        "Name": "fund_name",
        "ISIN": "isin",
        "CUSIP": "cusip",
        "Inception Date": "inception_date",
        "Asset Class": "asset_class",
        "Primary Index": "benchmark",
    }
    exp_doc_cols = ["Ticker", "Asset Class"]

    etf_holdings_url = (
        "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/"
        "etfs/us/holdings-daily-us-en-{}.xlsx"
    )

    etf_holdings_col_map = {
        "Name": "name",
        "Ticker": "ticker",
        "Identifier": "cusip",
        "SEDOL": "sedol",
        "Weight": "weight",
        "Sector": "sector",
        "Shares Held": "amount",
        "Local Currency": "market_currency",
    }

    holdings_na_rep = {"weight": ["-"], "amount": ["-"]}

    @classmethod
    def _query_ssga_fund_doc(cls) -> pd.DataFrame:
        """Query the document SSGA provides for ETF listings information such as
        asset class, exchange, isin. Not strictly necessary to query holdings information
        but useful if we want to filter on, eg Equity funds only.

        Note: doesn't cover mutual funds.
        """

        ssga_doc_req = requests.get(cls.ssga_doc_url)
        ssga_doc_req.raise_for_status()

        ssga_doc_df = pd.read_excel(BytesIO(ssga_doc_req.content), skiprows=1)
        check_missing_cols(cls.exp_doc_cols, ssga_doc_df.columns)

        ssga_doc_df = ssga_doc_df[~ssga_doc_df["Ticker"].isna()]
        ssga_doc_df_ = ssga_doc_df.reindex(columns=list(cls.ssga_doc_mapping)).rename(
            columns=cls.ssga_doc_mapping
        )
        return ssga_doc_df_

    @classmethod
    def _query_ssga_webpage(cls) -> pd.DataFrame:
        resp = requests.get(cls.ssga_web_url)
        resp.raise_for_status()

        ssga_web_data = resp.json()

        ssga_web_data_ = ssga_web_data["data"]["funds"]
        ssga_web_data_df = pd.concat(
            [
                pd.DataFrame(ssga_web_data_[i]["datas"]).assign(fund_type=j)
                for i, j in cls.ssga_products.items()
            ]
        )
        check_missing_cols(cls.exp_web_cols, ssga_web_data_df.columns, raise_error=True)
        reindex_cols = [*cls.ssga_web_resp_renaming, "fund_type"]

        ssga_web_data_df_ = ssga_web_data_df.reindex(columns=reindex_cols).rename(
            columns=cls.ssga_web_resp_renaming
        )

        def parse_aum(x):
            try:
                return float(x[-1]) * 1e6  # reported in MM
            except:
                return np.nan

        def parse_date(x):
            try:
                return datetime.strptime(x[-1], "%Y-%m-%d")
            except:
                return pd.NaT

        ssga_web_data_df_["inception_date"] = ssga_web_data_df_[
            "inception_date"
        ].apply(parse_date)
        ssga_web_data_df_["net_assets"] = ssga_web_data_df_["net_assets"].apply(
            parse_aum
        )
        ssga_web_data_df_["product_url"] = ssga_web_data_df_[
            "product_url"
        ].apply(lambda x: safe_urljoin(cls.host, x))
        return ssga_web_data_df_

    @classmethod
    def retrieve_listings(cls):
        """Retrievs SSGA listings for ETFS and Mutual Funds"""
        ssga_doc_df = cls._query_ssga_fund_doc()
        ssga_web_data_df = cls._query_ssga_webpage()

        ssga_doc_df_ = ssga_doc_df[
            ["ticker", "asset_class", "cusip", "isin", "benchmark"]
        ]
        ssga_listings = ssga_web_data_df.merge(ssga_doc_df_, how="left", on="ticker")
        ssga_listings["provider"] = cls.provider
        return ssga_listings

    @classmethod
    def _parse_holdings_resp(cls, resp_content):
        """Parse SSGA ETF holdings Excel
        These contain a preamble (with as of date, ticker etc) and the main
        holdings.

        Returns: a df of the holdings, and a date object representing the reported
        as of date and ticker
        """
        resp_df = pd.read_excel(BytesIO(resp_content))
        header_row = resp_df[resp_df.iloc[:, 0] == "Name"].index[0]
        preamble = resp_df.iloc[:header_row, :2].set_index(resp_df.columns[0]).squeeze()
        preamble.index = [str(x).rstrip(":").strip() for x in preamble.index]
        ticker = preamble["Ticker Symbol"]

        as_of_val = preamble["Holdings"]
        as_of_val = as_of_val.lower().split("as of")[-1].strip()
        as_of_date = datetime.strptime(as_of_val, "%d-%b-%Y").date()

        # return resp_df
        holdings_df = resp_df.iloc[header_row + 1 :]
        holdings_df.columns = resp_df.iloc[header_row].values
        holdings_df = holdings_df[~holdings_df["Ticker"].isna()]

        check_missing_cols(["Ticker", "Shares Held"], holdings_df.columns)

        holdings_df = holdings_df.reindex(
            columns=list(cls.etf_holdings_col_map)
        ).rename(columns=cls.etf_holdings_col_map)

        for col, na_vals in cls.holdings_na_rep.items():
            holdings_df[col] = holdings_df[col].replace(na_vals, np.nan)

        strip_str_cols(holdings_df, ["ticker"])
        set_numeric_cols(holdings_df, ["weight", "amount"])

        return holdings_df, as_of_date, ticker

    @classmethod
    def retrieve_holdings(cls, ticker: str) -> pd.DataFrame:
        holdings_url = cls.etf_holdings_url.format(ticker.lower())
        resp = requests.get(holdings_url)
        resp.raise_for_status()

        holdings_df, resp_holdings_date, resp_ticker = cls._parse_holdings_resp(
            resp.content
        )

        logger.debug(f"Found response as of date {resp_holdings_date} for {ticker}")

        if ticker.upper() != resp_ticker.upper():
            raise ValueError(
                f"Response ticker {resp_ticker} doesn't match the query ticker {ticker}"
            )

        holdings_df["fund_ticker"] = ticker
        holdings_df["as_of_date"] = pd.to_datetime(resp_holdings_date)
        return holdings_df

    @classmethod
    def _retrieve_holdings(
        cls, sec_listing: SecurityListing, holdings_date: Union[date, None]
    ) -> pd.DataFrame:
        _check_exp_provider(sec_listing.provider, cls.provider, cls.__name__)

        if holdings_date:
            raise InvalidParameterError(
                f"Can only query latest holdings (holdings_date=None) from SSGA"
            )

        if sec_listing.fund_type != "ETF":
            raise InvalidParameterError(
                f"Can only retrieve SSGA ETF holdings, not {sec_listing.fund_type}"
            )

        return cls.retrieve_holdings(sec_listing.ticker)
