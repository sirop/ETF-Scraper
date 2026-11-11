"""iShares (BlackRock) US listings and holdings scraper.

Sources (public, plain requests with an honest User-Agent):
- Fund list: the US product screener JSON.
- Holdings: each product page publishes <product page>/latest-holdings.csv.
  The older "<product page>/1467271812596.ajax?fileType=csv" download (which took
  an asOfDate) now returns an HTML page, so only the latest holdings are available
  from iShares. Past US holdings are available from SEC N-PORT filings.
"""

import logging
from typing import Union, List
from urllib.parse import urljoin
from datetime import date, datetime

import pandas as pd
import requests
import numpy as np

from etf_scraper.utils import check_missing_cols, safe_urljoin
from etf_scraper.parsers import parse_date, read_table, split_preamble, to_number
from etf_scraper.base import (
    Provider,
    ProviderListings,
    SecurityListing,
    InvalidParameterError,
)

from etf_scraper.http import ProviderSession
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)


class ISharesListings(ProviderListings):
    provider = Provider.IShares.value
    session = ProviderSession(Provider.IShares.value)
    host = "https://www.ishares.com"
    listing_endpoint = (
        "/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/product-screener-v3/"
        "data/en/us-ishares/ishares-product-screener-backend-config&siteEntryPassthrough=true"
    )
    holdings_file = "latest-holdings.csv"

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
        "Market Weight": "weight",  # equity funds, layout since 2026
        "Notional Weight": "notional_weight",
        "Type": "security_type",
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
        "notional_weight",
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

    # present in every layout (ETFs, bond funds, mutual funds)
    required_holding_cols = ["Name", "Market Value"]
    weight_cols = ["Weight (%)", "Market Weight"]
    _missing = {"-", "", "\xa0"}

    @classmethod
    def exp_holding_cols(cls, asset_class: str = "Equity") -> List[str]:
        """Columns every holdings download must have. The layouts no longer differ
        enough by asset class to check more than this."""
        return list(cls.required_holding_cols)

    @classmethod
    def retrieve_listings(cls):
        listing_url = urljoin(cls.host, cls.listing_endpoint)
        resp = cls.session.get(listing_url, expect=("json",))
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
        resp_df_["product_url"] = resp_df_["product_url"].apply(build_url)

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

        resp_df_["inception_date"] = resp_df_["inception_date"].apply(parse_date)
        resp_df_["net_assets"] = resp_df_["net_assets"].apply(parse_net)
        resp_df_["fund_type"] = resp_df_["fund_type"].apply(parse_fund_type)
        resp_df_["provider"] = cls.provider
        resp_df_["domicile"] = resp_df_["isin"].str[:2]
        return resp_df_.reset_index(drop=True)

    @classmethod
    def _parse_holdings_date(cls, date_str: str) -> date:
        return datetime.strptime(date_str, "%b %d, %Y").date()  # eg "Jan 03, 2022"

    @classmethod
    def _parse_date_or_none(cls, value):
        try:
            return cls._parse_holdings_date(str(value).strip())
        except ValueError:
            return None

    @classmethod
    def holdings_url(cls, product_url: str) -> str:
        return product_url.rstrip("/") + "/" + cls.holdings_file

    @classmethod
    def _parse_holdings_resp(cls, resp_content: str, asset_class: str = "Equity"):
        """Parse a holdings CSV download. Returns (holdings_df, as_of_date).

        Handles the ETF layout (9 preamble lines), the shorter mutual fund layout
        and the older layout with a single "Weight (%)" column.
        """
        metadata, table = split_preamble(resp_content, cls.exp_holding_cols(asset_class))

        as_of_str = next(
            (v for k, v in metadata.items() if "holdings as of" in k.lower()), None
        )
        if as_of_str is None:
            raise ValueError(
                f"Was expecting an 'as of date' indicator, instead found: {metadata}"
            )
        as_of_str = as_of_str.strip().strip("'\"")
        if as_of_str == "-":
            raise InvalidParameterError("Found '-' as holdings date -> no data returned")
        as_of_date = cls._parse_date_or_none(as_of_str)
        if as_of_date is None:
            raise ValueError(f"Couldn't parse the holdings date {as_of_str!r}")
        logger.debug(f"Found reported holdings date {as_of_date}")

        holdings_df = read_table(table)
        if not any(c in holdings_df.columns for c in cls.weight_cols):
            raise ValueError(
                f"No weight column found, columns: {list(holdings_df.columns)}"
            )

        # BUG (seen in older files): the content is sometimes repeated, starting
        # again with the fund name line. Keep only the first copy.
        doc_intro = resp_content.lstrip("\ufeff").split("\n", 1)[0].strip()
        first_col = holdings_df.iloc[:, 0].str.strip()
        dupl_ind = (first_col == doc_intro).to_numpy()
        if doc_intro and dupl_ind.any():
            logger.warning(
                f"Detected response contains {dupl_ind.sum()} duplicates, removing them"
            )
            holdings_df = holdings_df.iloc[: int(np.argmax(dupl_ind))]

        holdings_df = holdings_df.rename(columns=cls.holding_col_mapping)
        holdings_df = holdings_df.loc[:, ~holdings_df.columns.duplicated()].copy()

        for col in holdings_df.columns:
            if col in cls.holdings_numeric_cols:
                holdings_df[col] = to_number(holdings_df[col]).values
            elif col in cls.holdings_date_cols:
                parsed = [cls._parse_date_or_none(x) for x in holdings_df[col]]
                holdings_df[col] = pd.to_datetime(pd.Series(parsed, dtype="object")).values
            else:
                text = holdings_df[col].str.strip()
                holdings_df[col] = text.where(~text.isin(cls._missing), np.nan)

        # footer lines (disclaimers etc) have neither weight nor market value. Real
        # holdings can lack a weight ("-"), eg futures, collateral, unlisted stocks
        footer = holdings_df["weight"].isna() & holdings_df["market_value"].isna()
        holdings_df = holdings_df[~footer].reset_index(drop=True)
        return holdings_df, as_of_date

    @classmethod
    def retrieve_holdings(
        cls,
        ticker: str,
        product_url: str,
        holdings_date: Union[date, None] = None,
        asset_class: str = "Equity",
    ):
        """Query for iShares product holdings.

        iShares only publishes the latest holdings. If holdings_date is given, it
        must match the date of the latest file, otherwise InvalidParameterError is
        raised (past US holdings are in SEC N-PORT filings).
        """
        url = cls.holdings_url(product_url)
        logger.debug(f"Querying {cls.provider} for {ticker} holdings: {url}")
        try:
            resp = cls.session.get(url, expect=())
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (400, 404):
                raise InvalidParameterError(
                    f"{cls.provider} has no holdings download for {ticker} "
                    f"(HTTP {status}, fund possibly closed)"
                ) from e
            raise

        holdings_df, as_of_date = cls._parse_holdings_resp(
            resp.content.decode(encoding="UTF-8-SIG"), asset_class
        )

        if holdings_date and holdings_date != as_of_date:
            raise InvalidParameterError(
                f"{cls.provider} only provides the latest holdings (as of "
                f"{as_of_date}), not {holdings_date}. Past US holdings are "
                f"available from SEC N-PORT filings."
            )

        holdings_df["fund_ticker"] = ticker
        holdings_df["as_of_date"] = pd.to_datetime(as_of_date)
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
