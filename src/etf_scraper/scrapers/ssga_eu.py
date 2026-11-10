"""State Street (SPDR) European UCITS ETFs.

Sources (all public, plain requests with an honest User-Agent):
- Holdings: the daily holdings Excel file each fund page links to,
  /library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-<ticker>.xlsx
  where <ticker> is the fund's Bloomberg style ticker, eg "qus5-gy" for "QUS5 GY".
- Fund list: State Street's public fund finder JSON for Germany (all UCITS ETFs
  sold there), plus each fund page for its ISIN.

Only the latest holdings are available.
"""

import logging
import re
import warnings
from datetime import date
from io import BytesIO
from typing import Callable, Optional, Union

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
from etf_scraper.parsers import NA_VALUES, parse_date, parse_dates, to_number
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)


class SSGAEUListings(ProviderListings):
    provider = Provider.SSGA_EU.value
    session = ProviderSession(Provider.SSGA_EU.value)

    host = "https://www.ssga.com"
    fund_finder_url = (
        "https://www.ssga.com/bin/v1/ssmp/fund/fundfinder"
        "?country=de&language=de&role=intermediary&product=@all&ui=fund-finder"
    )
    holdings_url_fmt = (
        "https://www.ssga.com/library-content/products/fund-data/etfs/emea/"
        "holdings-daily-emea-en-{}.xlsx"
    )

    holding_col_mapping = {
        "ISIN": "isin",
        "SEDOL": "sedol",
        "Security Name": "name",
        "Currency": "currency",
        "Currency Local": "currency",
        "Number of Shares": "amount",
        "PAR Value Local": "amount",
        "Percent of Fund": "weight",
        "Trade Country Name": "location",
        "Country of Issue": "location",
        "Local Price": "price",
        "Sector Classification": "sector",
        "Industry Classification": "industry",
        "Base Market Value": "market_value",
        "Maturity Date": "maturity_date",
        "Interest Rate": "coupon",
    }
    numeric_cols = ["amount", "weight", "price", "market_value", "coupon"]
    date_cols = ["maturity_date"]
    id_na_values = NA_VALUES + ["Unassigned"]

    # ---------------------------------------------------------------- holdings

    @staticmethod
    def ticker_slug(bloomberg_ticker: str) -> str:
        """'QUS5 GY' -> 'qus5-gy' (a trailing ^ in the fund finder is dropped)."""
        return bloomberg_ticker.replace("^", "").strip().lower().replace(" ", "-")

    @classmethod
    def holdings_url(cls, bloomberg_ticker: str) -> str:
        return cls.holdings_url_fmt.format(cls.ticker_slug(bloomberg_ticker))

    @classmethod
    def _parse_holdings_resp(cls, content: bytes):
        """Parse a holdings-daily-emea xlsx. Returns (holdings_df, as_of_date, isin)."""
        with warnings.catch_warnings():
            # these workbooks have no default style, which openpyxl warns about
            warnings.simplefilter("ignore", UserWarning)
            raw = pd.read_excel(BytesIO(content), header=None, dtype=str)

        first_col = raw[0].astype(str).str.strip()
        header_rows = raw.index[
            (first_col == "ISIN") & raw.apply(lambda r: "Security Name" in r.values, axis=1)
        ]
        if len(header_rows) == 0:
            raise ValueError("No holdings table header (ISIN, ..., Security Name) found")
        header_idx = header_rows[0]

        metadata = {}
        for _, row in raw.iloc[:header_idx].iterrows():
            key = str(row[0]).strip().rstrip(":")
            if key and key != "nan":
                metadata[key] = None if pd.isna(row[1]) else str(row[1]).strip()

        as_of_date = parse_date(metadata.get("Holdings As Of"), formats=("%d-%b-%Y",))
        if as_of_date is None:
            raise ValueError(f"Was expecting a 'Holdings As Of' date, found {metadata}")

        table = raw.iloc[header_idx + 1:].copy()
        table.columns = [str(c).strip() for c in raw.iloc[header_idx]]
        # the table ends at the first empty row (a disclaimer follows)
        empty = table.isna().all(axis=1)
        if empty.any():
            table = table.loc[: empty.idxmax()].iloc[:-1]

        holdings_df = table.rename(columns=cls.holding_col_mapping).reset_index(drop=True)
        holdings_df = holdings_df.loc[:, ~holdings_df.columns.duplicated()]

        for col in ["isin", "sedol", "sector", "industry"]:
            if col in holdings_df:
                holdings_df[col] = holdings_df[col].mask(
                    holdings_df[col].isin(cls.id_na_values)
                )
        for col in cls.numeric_cols:
            if col in holdings_df:
                holdings_df[col] = to_number(holdings_df[col], thousands=None).values
        for col in cls.date_cols:
            if col in holdings_df:
                holdings_df[col] = parse_dates(
                    holdings_df[col], formats=("%d-%b-%Y",)
                ).values

        holdings_df["as_of_date"] = pd.Timestamp(as_of_date)
        return holdings_df, as_of_date, metadata.get("ISIN")

    @classmethod
    def retrieve_holdings(
        cls, ticker: str, bloomberg_ticker: str, isin: Optional[str] = None
    ) -> pd.DataFrame:
        """Retrieve the latest holdings from the fund's daily holdings file."""
        url = cls.holdings_url(bloomberg_ticker)
        logger.debug(f"Querying {cls.provider} for {ticker} holdings: {url}")
        resp = cls.session.get(url, expect=("spreadsheet", "excel", "octet-stream"))
        holdings_df, _, file_isin = cls._parse_holdings_resp(resp.content)

        if isin and file_isin and isin != file_isin:
            raise ValueError(
                f"{cls.provider}: holdings file is for {file_isin}, expected {isin}"
            )
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
        bloomberg_ticker = sec_listing.product_id
        if not isinstance(bloomberg_ticker, str) or not bloomberg_ticker:
            raise InvalidParameterError(
                f"No exchange ticker (product_id, eg 'SPY5 GY') known for {sec_listing.ticker}"
            )
        isin = sec_listing.isin if isinstance(sec_listing.isin, str) else None
        return cls.retrieve_holdings(sec_listing.ticker, bloomberg_ticker, isin)

    # ---------------------------------------------------------------- listings

    @classmethod
    def _query_fund_finder(cls) -> list:
        resp = cls.session.get(cls.fund_finder_url, expect=("json",))
        return resp.json()["data"]["funds"]["etfs"]["datas"]

    @staticmethod
    def _parse_fund_page_isin(page: str) -> Optional[str]:
        """The ISIN that appears most often on a fund page (other funds' ISINs can
        appear in related fund links)."""
        found = [
            x for x in re.findall(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b", page)
            if x[:2] in ("IE", "LU") and is_valid_isin(x)
        ]
        if not found:
            return None
        return pd.Series(found).value_counts().index[0]

    @classmethod
    def retrieve_listings(
        cls,
        max_funds: Optional[int] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> pd.DataFrame:
        """Build the State Street UCITS ETF listings.

        Uses the fund finder for the fund list and fetches each fund page for its
        ISIN (~180 pages at the session rate limit).
        """
        funds = cls._query_fund_finder()
        if max_funds:
            funds = funds[:max_funds]

        rows = []
        for i, fund in enumerate(funds, 1):
            bbg = fund["fundTicker"].replace("^", "").strip()
            url = cls.host + fund["fundUri"]
            if progress:
                progress(i, len(funds), url)
            try:
                isin = cls._parse_fund_page_isin(cls.session.get(url, expect=("html",)).text)
            except Exception as e:
                logger.warning(f"{cls.provider}: no ISIN for {bbg} ({type(e).__name__}: {e})")
                isin = None

            inception = fund.get("inceptionDate")
            inception = inception[1] if isinstance(inception, list) and len(inception) > 1 else None
            rows.append(
                {
                    "ticker": bbg.split()[0],
                    "fund_name": fund.get("fundName"),
                    "isin": isin,
                    "product_id": bbg,
                    "product_url": url,
                    "exchange": fund.get("primaryExchange"),
                    "domicile": fund.get("domicile") or (isin[:2] if isin else np.nan),
                    "inception_date": inception,
                }
            )

        listings = pd.DataFrame(rows)
        listings["fund_type"] = "ETF"
        listings["provider"] = cls.provider
        return listings
