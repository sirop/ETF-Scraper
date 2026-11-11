"""HSBC UCITS ETFs, via assetmanagement.hsbc.de.

Sources (plain GET requests with an honest User-Agent; robots.txt asks for
Crawl-delay: 2, so at most one request per 2.5 seconds):
- Holdings: the "holdings" document each fund page links,
  /api/v1/download/document/<isin>/de/de/holdings, an .xls (old Excel format)
  with fund name, date and fund size above a table of ISIN, CUSIP, name, shares,
  market value, country, currency and weight per holding.
- Fund list: HSBC's own fund list is only available through a token protected
  API with a quota, which we don't use. Instead the package ships
  data/hsbc_etfs.csv, a list of share class ISINs and names taken from HSBC ETFs
  plc's published AGM documents. retrieve_listings checks each ISIN against its
  public fund page (/de/individual-investors/funds/<isin>) and keeps the share
  classes whose page exists and links a holdings document.

The fund pages don't show tickers without the API, so HSBC funds are looked up
by ISIN. Holdings are per fund. Only the latest holdings are available.
"""

import importlib.util
import logging
import re
from datetime import date, datetime
from io import BytesIO
from typing import Optional, Union

import numpy as np
import pandas as pd

from etf_scraper.base import (
    InvalidParameterError,
    Provider,
    ProviderListings,
    SecurityListing,
)
from etf_scraper.config import HSBC_SEED_FILE
from etf_scraper.http import ProviderSession
from etf_scraper.identifiers import is_valid_isin
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)

HOLDINGS_HEADER = "ISIN"
HOLDING_COL_MAPPING = {
    "ISIN": "isin",
    "CUSIP": "cusip",
    "SecurityName": "name",
    "NumberOfShare": "amount",
    "MarketValue": "market_value",
    "Country": "location",
    "LocalCurrencyCode": "currency",
    "Weighting": "weight",
}


def _excel_engine(content: bytes) -> str:
    if content[:2] == b"PK":  # a zip archive: modern .xlsx
        return "openpyxl"
    # old .xls: pandas prefers calamine; fall back to xlrd (a package dependency)
    return "calamine" if importlib.util.find_spec("python_calamine") else "xlrd"


class HSBCListings(ProviderListings):
    provider = Provider.HSBC.value
    session = ProviderSession(Provider.HSBC.value, min_interval=2.5)

    host = "https://www.assetmanagement.hsbc.de"
    fund_page = host + "/de/individual-investors/funds/{isin}"
    holdings_url = host + "/api/v1/download/document/{isin}/de/de/holdings"

    # ---------------------------------------------------------------- holdings

    @staticmethod
    def _parse_header_date(value) -> Optional[pd.Timestamp]:
        if isinstance(value, (datetime, date, pd.Timestamp)):
            return pd.Timestamp(value).normalize()
        if isinstance(value, (int, float)) and not pd.isna(value):
            # Excel serial date
            return pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(value))
        if isinstance(value, str) and value.strip():
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
                try:
                    return pd.Timestamp(datetime.strptime(value.strip()[:10], fmt))
                except ValueError:
                    continue
        return None

    @classmethod
    def _parse_holdings_resp(cls, content: bytes) -> pd.DataFrame:
        """Parse the holdings .xls into a holdings frame."""
        raw = pd.read_excel(BytesIO(content), header=None, engine=_excel_engine(content))
        first_col = raw.iloc[:, 0].astype(str).str.strip()
        header_rows = raw.index[first_col == HOLDINGS_HEADER]
        if len(header_rows) == 0:
            raise InvalidParameterError(f"{cls.provider}: no holdings table in the file")
        header_row = header_rows[0]

        meta = {
            str(k).strip(): v
            for k, v in zip(raw.iloc[:header_row, 0], raw.iloc[:header_row, 1])
            if isinstance(k, str)
        }
        as_of_date = cls._parse_header_date(meta.get("Date"))

        df = raw.iloc[header_row + 1 :].copy()
        df.columns = [str(c).strip() for c in raw.iloc[header_row]]
        df = df.dropna(how="all").rename(columns=HOLDING_COL_MAPPING)
        if df.empty:
            raise InvalidParameterError(f"{cls.provider}: no holdings published")
        for col in ["amount", "market_value", "weight"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ["isin", "cusip", "name", "location", "currency"]:
            df[col] = df[col].astype("object").where(df[col].notna(), np.nan)
            df[col] = df[col].map(lambda v: str(v).strip() if isinstance(v, str) else v)
        df["as_of_date"] = as_of_date
        df["fund_name"] = meta.get("Name")
        df["fund_size"] = pd.to_numeric(meta.get("Fund Size"), errors="coerce")
        return df.reset_index(drop=True)

    @classmethod
    def retrieve_holdings(cls, isin: str, ticker: str = "") -> pd.DataFrame:
        """Retrieve the latest holdings of the fund a share class ISIN belongs to."""
        url = cls.holdings_url.format(isin=isin.lower())
        logger.debug(f"Querying {cls.provider} for {isin} holdings: {url}")
        try:
            resp = cls.session.get(url, expect=("xls", "excel", "spreadsheet", "octet-stream"))
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                raise InvalidParameterError(
                    f"{cls.provider}: no holdings document for {isin}"
                ) from e
            raise
        df = cls._parse_holdings_resp(resp.content)
        df["fund_ticker"] = ticker or np.nan
        return df

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
        return cls.retrieve_holdings(sec_listing.isin, sec_listing.ticker)

    # ---------------------------------------------------------------- listings

    @staticmethod
    def load_seed(path=None) -> pd.DataFrame:
        seed = pd.read_csv(path or HSBC_SEED_FILE, dtype=str, keep_default_na=False)
        seed["isin"] = seed["isin"].str.strip().str.upper()
        return seed[seed["isin"].map(is_valid_isin)].drop_duplicates("isin")

    @classmethod
    def parse_fund_page(cls, isin: str, html: str) -> Optional[dict]:
        """Name and holdings availability from a fund page, or None if the page
        is not this share class's page (eg a closed share class)."""
        share_id = re.search(r"shareId:\s*'([A-Z0-9]{12})'", html)
        if not share_id or share_id[1].upper() != isin.upper():
            return None
        title = re.search(r"<title>\s*([^<]+?)\s*</title>", html)
        return {
            "page_name": title[1] if title else None,
            "has_holdings": f"/api/v1/download/document/{isin.lower()}/de/de/holdings" in html,
        }

    @classmethod
    def retrieve_listings(cls, seed: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """Check each seed ISIN against its fund page (one GET per ISIN)."""
        seed = cls.load_seed() if seed is None else seed
        rows = []
        for isin, seed_name in zip(seed["isin"], seed["fund_name"]):
            url = cls.fund_page.format(isin=isin.lower())
            try:
                resp = cls.session.get(url, expect=("html",))
            except Exception as e:
                logger.warning(f"{cls.provider}: skipping {isin}: {e}")
                continue
            info = cls.parse_fund_page(isin, resp.text)
            if info is None or not info["has_holdings"]:
                logger.info(f"{cls.provider}: {isin} has no fund page or holdings, skipped")
                continue
            rows.append(
                {
                    "ticker": np.nan,
                    "fund_name": seed_name or info["page_name"],
                    "isin": isin,
                    "domicile": isin[:2],
                    "product_url": url,
                    "fund_type": "ETF",
                    "provider": cls.provider,
                }
            )
        cols = ["ticker", "fund_name", "isin", "domicile", "product_url", "fund_type", "provider"]
        return pd.DataFrame(rows, columns=cols)
