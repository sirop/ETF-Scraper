"""Xtrackers (DWS) UCITS ETFs, via etf.deutsche-am.com.

Sources (plain GET requests with an honest User-Agent, no cookies; robots.txt
disallows /etfdata/ and /export/, which are not used):
- Holdings: the JSON the product page's holdings table loads,
  /api/pdp/de-de/etf/<ISIN>/holdings: ISIN, name, weight, market value (fund
  currency), country, sector and asset class per holding; the date is in the
  table's source note ("Quelle: DWS 09.11.2026"). The full Excel download links to
  /etfdata/ and is not used.
- Fund list: the German sitemap (/de-de/sitemap.xml) lists every product page as
  /de-de/<ISIN>-<name>/. Names are taken from that address.

Other product data (eg tickers) is only served after the site's investor type
question, which we don't answer, so Xtrackers funds are looked up by ISIN. Holdings
are per fund. Only the latest holdings are available.

Terms of use: downloading content needs DWS's written consent "unless permitted by
law" (section 5). Personal use is the user's responsibility; don't redistribute.
"""

import logging
import re
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
from etf_scraper.identifiers import is_valid_isin
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)

# column labels of the holdings table (German site)
HOLDING_COL_MAPPING = {
    "ISIN": "isin",
    "Name": "name",
    "Gewichtung %": "weight",
    "Marktwert": "market_value",
    "Land": "location",
    "Industrie": "sector",
    "Anlageklasse": "security_type",
}
NUMERIC_COLS = ["weight", "market_value"]
_MISSING = {"", "--", "-", "unbekannt"}
PRODUCT_URL_RE = re.compile(r"/de-de/([A-Z]{2}[A-Z0-9]{9}[0-9])-([a-z0-9-]+)/?$")
# words kept in capitals when building a name from the page address
_UPPER = {"msci", "usd", "eur", "gbp", "chf", "jpy", "esg", "etf", "ucits", "us", "uk",
          "em", "csi300", "dax", "mdax", "sdax", "tecdax", "ftse", "atx", "smi",
          "ii", "sri", "pab", "ctb", "eu", "s&p", "dj", "gcc", "asean", "ai", "hy",
          "ig", "x", "sdg", "etc"}


class XtrackersListings(ProviderListings):
    provider = Provider.Xtrackers.value
    session = ProviderSession(Provider.Xtrackers.value, min_interval=2.0)

    host = "https://etf.deutsche-am.com"
    sitemap_url = host + "/de-de/sitemap.xml"
    holdings_url = host + "/api/pdp/de-de/etf/{isin}/holdings"

    # ---------------------------------------------------------------- holdings

    @staticmethod
    def _cell(row: dict, key: str):
        cell = row.get(key) or {}
        if "sortValue" in cell:
            return cell["sortValue"]
        value = cell.get("value")
        if isinstance(value, str):
            value = value.strip()
            return None if value in _MISSING else value
        return value

    @classmethod
    def _parse_holdings_resp(cls, data: dict) -> pd.DataFrame:
        tables = data.get("tables") or []
        if not tables or not tables[0].get("values"):
            raise InvalidParameterError(f"{cls.provider}: no holdings published")
        table = tables[0]
        labels = {c["key"]: c["value"] for c in table.get("columns", [])}
        unknown = [v for v in labels.values() if v not in HOLDING_COL_MAPPING]
        if unknown:
            logger.debug(f"{cls.provider}: keeping unmapped columns {unknown}")
        if "ISIN" not in labels.values() or "Gewichtung %" not in labels.values():
            raise InvalidParameterError(
                f"{cls.provider}: unexpected holdings columns {list(labels.values())}"
            )

        rows = [
            {HOLDING_COL_MAPPING.get(label, label): cls._cell(row, key)
             for key, label in labels.items()}
            for row in table["values"]
        ]
        df = pd.DataFrame(rows)
        for col in NUMERIC_COLS:
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # market values are shown as eg "67,72 Mio. USD": the fund currency
        currency = None
        mv_key = next((k for k, v in labels.items() if v == "Marktwert"), None)
        for row in table["values"][:20]:
            text = ((row.get(mv_key) or {}).get("value") or "") if mv_key else ""
            m = re.search(r"\b([A-Z]{3})\s*$", text)
            if m:
                currency = m[1]
                break
        df["currency"] = currency

        # cash lines carry pseudo identifiers like "_CURRENCYUSD"
        isin = df["isin"].astype("object")
        cash = isin.str.startswith("_CURRENCY", na=False)
        df.loc[cash, "currency"] = isin[cash].str[len("_CURRENCY"):]
        df["isin"] = isin.where(isin.map(lambda v: isinstance(v, str) and is_valid_isin(v)), np.nan)

        as_of_date = None
        for note in table.get("disclaimers") or []:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", note.get("text") or "")
            if m:
                as_of_date = pd.Timestamp(int(m[3]), int(m[2]), int(m[1]))
                break
        df["as_of_date"] = as_of_date
        return df

    @classmethod
    def retrieve_holdings(cls, isin: str) -> pd.DataFrame:
        """Retrieve the latest holdings of the fund a share class ISIN belongs to."""
        url = cls.holdings_url.format(isin=isin.upper())
        logger.debug(f"Querying {cls.provider} for {isin} holdings: {url}")
        try:
            resp = cls.session.get(url, expect=("json",))
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                raise InvalidParameterError(f"{cls.provider}: no holdings for {isin}") from e
            raise
        df = cls._parse_holdings_resp(resp.json())
        df["fund_ticker"] = np.nan  # tickers aren't available, see module docstring
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
        return cls.retrieve_holdings(sec_listing.isin)

    # ---------------------------------------------------------------- listings

    @staticmethod
    def _name_from_slug(slug: str) -> str:
        """'msci-world-ucits-etf-1c' -> 'Xtrackers MSCI World UCITS ETF 1C'"""
        slug = re.sub(r"(^|-)s-p(-|$)", r"\1s&p\2", slug)  # "s-p" stands for S&P
        words = [
            w.upper() if w in _UPPER or re.fullmatch(r"\d+[a-z]{0,2}", w) else w.capitalize()
            for w in slug.split("-")
        ]
        return "Xtrackers " + " ".join(words)

    @classmethod
    def parse_sitemap(cls, xml: str) -> pd.DataFrame:
        rows = []
        for url in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            m = PRODUCT_URL_RE.search(url)
            if not m or not is_valid_isin(m[1]):
                continue
            rows.append({
                "ticker": np.nan,
                "fund_name": cls._name_from_slug(m[2]),
                "isin": m[1],
                "domicile": m[1][:2],
                "product_url": url,
                "fund_type": "ETF",
                "provider": cls.provider,
            })
        cols = ["ticker", "fund_name", "isin", "domicile", "product_url", "fund_type", "provider"]
        return pd.DataFrame(rows, columns=cols).drop_duplicates("isin").reset_index(drop=True)

    @classmethod
    def retrieve_listings(cls) -> pd.DataFrame:
        """All Xtrackers ETF share classes from the German sitemap (one request)."""
        resp = cls.session.get(cls.sitemap_url, expect=("xml",))
        return cls.parse_sitemap(resp.text)
