import logging
from typing import Union, List
from urllib.parse import urljoin
from io import StringIO
from datetime import date, datetime

from dateutil.relativedelta import relativedelta
import requests
import pandas as pd
import numpy as np

from etf_scraper.utils import (
    check_missing_cols,
    check_data_mismatch,
    check_dupl_cols,
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

logger = logging.getLogger(__name__)


def _check_exp_provider(given, exp, class_name):
    if given != exp:
        raise ValueError(
            f"Provider should be {exp}, not {given} for class {class_name}"
        )


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
                holdings_df.loc[:, col] = pd.to_datetime(parsed_dates)
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

        ssga_doc_df = pd.read_excel(ssga_doc_req.content, skiprows=1)
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

        ssga_web_data_df_.loc[:, "inception_date"] = ssga_web_data_df_[
            "inception_date"
        ].apply(parse_date)
        ssga_web_data_df_.loc[:, "net_assets"] = ssga_web_data_df_["net_assets"].apply(
            parse_aum
        )
        ssga_web_data_df_.loc[:, "product_url"] = ssga_web_data_df_[
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
        ssga_listings.loc[:, "provider"] = cls.provider
        return ssga_listings

    @classmethod
    def _parse_holdings_resp(cls, resp_content):
        """Parse SSGA ETF holdings Excel
        These contain a preamble (with as of date, ticker etc) and the main
        holdings.

        Returns: a df of the holdings, and a date object representing the reported
        as of date and ticker
        """
        resp_df = pd.read_excel(resp_content)
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
            holdings_df.loc[:, col] = holdings_df[col].replace(na_vals, np.nan)

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

        holdings_df.loc[:, "fund_ticker"] = ticker
        holdings_df.loc[:, "as_of_date"] = pd.to_datetime(resp_holdings_date)
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

        fund_df_.loc[:, "fund_type"] = fund_types

        map_to_url = lambda x: cls.fund_url.format(x.upper())
        fund_df_.loc[:, "product_url"] = fund_df_["ticker"].apply(map_to_url)

        def parse_inception_date(x):
            try:
                return datetime.fromisoformat(x).date()
            except:
                return pd.NaT

        fund_df_.loc[:, "inception_date"] = fund_df_["inception_date"].apply(
            parse_inception_date
        )
        fund_df_.loc[:, "provider"] = cls.provider

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

        holdings_df.loc[:, "as_of_date"] = holdings_df["as_of_date"].apply(
            cls._parse_vanguard_date
        )
        holdings_df["as_of_date"] = pd.to_datetime(holdings_df["as_of_date"])
        holdings_df["maturity_date"] = pd.to_datetime(
            holdings_df["maturity_date"].apply(cls._parse_vanguard_date)
        )
        holdings_df.loc[:, "holding_category"] = category

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
        holdings_df.loc[:, "fund_ticker"] = fund_ticker
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
    # Invesco's firewall rejects clients imitating a browser, so identify honestly
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "etf-scraper (+https://github.com/nikulpatel3141/ETF-Scraper)",
    }

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
        resp = requests.get(cls.search_endpoint, params=params, headers=cls.req_headers)
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
        listings_df_.loc[:, "inception_date"] = pd.to_datetime(
            listings_df_["inception_date"], errors="coerce"
        ).dt.date
        listings_df_.loc[:, "product_url"] = listings_df_["ticker"].apply(
            lambda x: cls.item_url.format(x)
        )
        listings_df_.loc[:, "fund_type"] = "ETF"
        listings_df_.loc[:, "provider"] = cls.provider

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
        holdings_df_.loc[:, "as_of_date"] = as_of_date

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
        resp = requests.get(url, params=params, headers=cls.req_headers)
        resp.raise_for_status()

        holdings_df, _, ret_cusip = cls._parse_holdings_resp(resp.json())
        if ret_cusip:
            check_data_mismatch(cusip, ret_cusip, "cusip", raise_error=True)

        holdings_df.loc[:, "fund_ticker"] = ticker.upper()
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
