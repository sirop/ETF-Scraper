"""VanEck UCITS ETFs, via the VanEck Ireland site (www.vaneck.com/ie/en/).

Sources (plain GET requests with an honest User-Agent; robots.txt asks for
Crawl-delay: 25, so at most one request per 25 seconds):
- Holdings: the "Download All Holdings" Excel file of each fund,
  /ie/en/investments/<fund slug>/downloads/holdings/, with name, ticker, ISIN,
  shares, market value and % of net assets per holding. The sheet name carries the
  fund ticker and the date, eg "SMH_asof_20261109".
- Fund list: VanEck's fund explorer loads its list with a POST that only answers
  browser sessions, and the site has no sitemap. Instead the package ships
  data/vaneck_eu_etfs.csv, a list of fund page slugs. It is built from the ETF
  list page saved in a browser (scripts/update_vaneck_seed.py). retrieve_listings
  reads name, ISIN and ticker from each fund page's schema.org data.

Use the /ie/en/ paths: /ucits/ redirects by location (to German number formats
from Germany). Only the latest holdings are available.
"""

import json
import logging
import re
import warnings
from datetime import date
from io import BytesIO
from typing import Callable, List, Optional, Union

import numpy as np
import pandas as pd

from etf_scraper.base import (
    InvalidParameterError,
    Provider,
    ProviderListings,
    SecurityListing,
)
from etf_scraper.config import VANECK_EU_SEED_FILE
from etf_scraper.http import ProviderSession
from etf_scraper.identifiers import is_valid_isin
from etf_scraper.parsers import to_number
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)

HOLDINGS_HEADER = "Holding Name"
HOLDING_COL_MAPPING = {
    "Holding Name": "name",
    "Ticker": "bbg_ticker",
    "ISIN": "isin",
    "Shares": "amount",
    "Market Value": "market_value",
    "% of Net Assets": "weight",
}
# fund page links on any VanEck country site, eg /ie/en/ or /de/de/ (same slugs)
SLUG_RE = re.compile(r"/(?:[a-z]{2,4})/(?:[a-z]{2})/investments/([a-z0-9][a-z0-9-]*)/")


class VanEckEUListings(ProviderListings):
    provider = Provider.VanEck_EU.value
    session = ProviderSession(Provider.VanEck_EU.value, min_interval=25.0)

    host = "https://www.vaneck.com"
    fund_page = host + "/ie/en/investments/{slug}/"
    holdings_url = fund_page + "downloads/holdings/"

    # ---------------------------------------------------------------- holdings

    @staticmethod
    def _parse_sheet_name(sheet_name: str):
        """("SMH", Timestamp(2026-11-09)) from "SMH_asof_20261109"."""
        m = re.match(r"^(.*)_asof_(\d{8})$", sheet_name.strip())
        if not m:
            return None, None
        return m[1], pd.Timestamp(m[2])

    @staticmethod
    def _to_number(values) -> pd.Series:
        """'$ 915,245,145.00', '11.61%', '-€ 1,234.50', '(1,234)' -> floats"""
        s = pd.Series(values, dtype="object").astype(str).str.strip()
        negative = s.str.startswith("(") | s.str.contains(r"^-|^[^\d]*-", regex=True)
        digits = s.str.replace(r"[^\d.,]", "", regex=True)
        out = to_number(digits)
        return out.where(~negative, -out)

    @classmethod
    def _parse_holdings_resp(cls, content: bytes) -> pd.DataFrame:
        if content[:2] != b"PK":
            raise InvalidParameterError(f"{cls.provider}: the holdings file isn't an Excel file")
        with warnings.catch_warnings():  # "Workbook contains no default style"
            warnings.simplefilter("ignore", UserWarning)
            sheets = pd.read_excel(BytesIO(content), header=None, sheet_name=None,
                                   dtype=str, engine="openpyxl")
        sheet_name, raw = next(iter(sheets.items()))
        fund_ticker, as_of_date = cls._parse_sheet_name(sheet_name)

        first_rows = raw.fillna("").astype(str)
        header_rows = [i for i, row in first_rows.iterrows() if HOLDINGS_HEADER in row.values]
        if not header_rows:
            raise InvalidParameterError(f"{cls.provider}: no holdings table in the file")
        header_row = header_rows[0]
        if as_of_date is None:  # "All Holdings  11/09/2026" above the table
            for cell in first_rows.iloc[:header_row, 0]:
                m = re.search(r"(\d{2})/(\d{2})/(\d{4})", cell)
                if m:
                    as_of_date = pd.Timestamp(int(m[3]), int(m[1]), int(m[2]))
                    break

        df = raw.iloc[header_row + 1 :].copy()
        df.columns = [str(c).strip() for c in raw.iloc[header_row]]
        df = df.rename(columns=HOLDING_COL_MAPPING)
        df = df[df["name"].notna() & (df["name"].astype(str).str.strip() != "")]
        if df.empty:
            raise InvalidParameterError(f"{cls.provider}: no holdings published")

        for col in ["amount", "market_value", "weight"]:
            if col in df:
                df[col] = cls._to_number(df[col]).values
        for col in ["name", "bbg_ticker", "isin"]:
            if col in df:
                text = df[col].astype("object").map(
                    lambda v: v.strip() if isinstance(v, str) else v
                )
                df[col] = text.where(~text.isin(["", "-", "--"]), np.nan)
        if "bbg_ticker" in df:
            # Bloomberg style "AMD US": ticker and exchange code
            parts = df["bbg_ticker"].str.rsplit(" ", n=1)
            df["ticker"] = parts.str[0]
            df["exchange"] = parts.str[1]
        if "Number" in df:
            df = df.drop(columns="Number")

        df["as_of_date"] = as_of_date
        df["file_fund_ticker"] = fund_ticker
        return df.reset_index(drop=True)

    @classmethod
    def retrieve_holdings(cls, slug: str, ticker: str = "") -> pd.DataFrame:
        """Retrieve the latest holdings of the fund with this fund page slug."""
        url = cls.holdings_url.format(slug=slug)
        logger.debug(f"Querying {cls.provider} for {ticker or slug} holdings: {url}")
        try:
            resp = cls.session.get(url, expect=("spreadsheet", "excel", "octet-stream"))
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                raise InvalidParameterError(f"{cls.provider}: no holdings for {slug}") from e
            raise
        df = cls._parse_holdings_resp(resp.content)
        df["fund_ticker"] = ticker or df["file_fund_ticker"]
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
        slug = sec_listing.product_id
        if not isinstance(slug, str) or not slug:
            m = SLUG_RE.search(str(sec_listing.product_url or ""))
            slug = m[1] if m else None
        if not slug:
            raise InvalidParameterError(f"No fund page known for {sec_listing.ticker}")
        return cls.retrieve_holdings(slug, sec_listing.ticker)

    # ---------------------------------------------------------------- listings

    @staticmethod
    def slugs_from_html(html: str) -> List[str]:
        """Fund page slugs linked from a (saved) VanEck page, in order of appearance."""
        seen = []
        for slug in SLUG_RE.findall(html):
            if slug not in seen:
                seen.append(slug)
        return seen

    @staticmethod
    def load_seed(path=None) -> pd.DataFrame:
        seed = pd.read_csv(path or VANECK_EU_SEED_FILE, dtype=str, keep_default_na=False)
        seed["slug"] = seed["slug"].str.strip().str.strip("/")
        return seed[seed["slug"] != ""].drop_duplicates("slug")

    @staticmethod
    def parse_fund_page(html: str) -> Optional[dict]:
        """Name, ISIN, ticker and category from a fund page, or None if the page
        doesn't describe a UCITS fund."""
        info = {}
        for block in re.findall(
            r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S
        ):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if "InvestmentFund" in types and not info.get("isin"):
                    info["fund_name"] = node.get("name")
                    info["isin"] = node.get("identifier")
                    info["category"] = node.get("category")
                elif "WebPage" in types and node.get("name"):
                    # "Semiconductor ETF | SMH | VanEck"
                    parts = [p.strip() for p in node["name"].replace("\xa0", " ").split("|")]
                    if len(parts) >= 3:
                        info["ticker"] = parts[-2]
        if not isinstance(info.get("isin"), str) or not is_valid_isin(info["isin"]):
            return None
        return info

    @classmethod
    def retrieve_listings(
        cls,
        seed: Optional[pd.DataFrame] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> pd.DataFrame:
        """Read each seed fund page (one GET per fund, 25 s apart: ~40 minutes for
        90 funds)."""
        seed = cls.load_seed() if seed is None else seed
        rows = []
        slugs = list(seed["slug"])
        for i, slug in enumerate(slugs, 1):
            if progress:
                progress(i, len(slugs), slug)
            url = cls.fund_page.format(slug=slug)
            try:
                resp = cls.session.get(url, expect=("html",))
            except Exception as e:
                logger.warning(f"{cls.provider}: skipping {slug}: {e}")
                continue
            info = cls.parse_fund_page(resp.text)
            if info is None:
                logger.info(f"{cls.provider}: {slug} is not a UCITS fund page, skipped")
                continue
            rows.append(
                {
                    "ticker": info.get("ticker") or np.nan,
                    "fund_name": info["fund_name"],
                    "isin": info["isin"],
                    "region": info.get("category") or np.nan,  # eg "Global"
                    "product_url": url,
                    "product_id": slug,
                    "fund_type": "ETF",
                    "provider": cls.provider,
                    "domicile": info["isin"][:2],
                }
            )
        cols = ["ticker", "fund_name", "isin", "region", "product_url", "product_id",
                "fund_type", "provider", "domicile"]
        return pd.DataFrame(rows, columns=cols)
