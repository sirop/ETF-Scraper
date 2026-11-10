"""Amundi UCITS ETFs (including the former Lyxor range), via amundietf.de.

Sources (plain requests with an honest User-Agent; robots.txt only disallows search
query strings):
- Holdings: the product pages are a JavaScript widget that loads all fund data,
  holdings included, with POST /mapi/ProductAPI/getProductsData. We send the same
  request, with the context the public German retail pages
  (/de/privatanleger/...) embed in their own HTML:
  {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"}.
  This is the data every visitor of those pages sees; no professional investor
  status is claimed.
  Each holding has its date, type, Bloomberg ticker, ISIN, name, weight (as a
  fraction), quantity, currency, sector and country of risk.
- Fund list: the sitemap lists the product pages
  (/de/professionell/products/<asset class>/<slug>/<isin>; the same pages exist
  under /de/privatanleger/); names and tickers come
  from the same API, in batches.

Synthetic (swap based) ETFs report their substitute basket. Only the latest
holdings are available.
"""

import logging
import re
from datetime import date
from typing import Dict, List, Optional, Union

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

PRODUCT_URL_RE = re.compile(
    r"^https://www\.amundietf\.de/de/(?:privatanleger|professionell)/products/"
    r"(?P<asset_class>[^/]+)/(?P<slug>[^/]+)/(?P<isin>[a-z]{2}[a-z0-9]{9}[0-9])/?$",
    re.I,
)


class AmundiListings(ProviderListings):
    provider = Provider.Amundi.value
    session = ProviderSession(
        Provider.Amundi.value, headers={"Accept": "application/json"}
    )

    host = "https://www.amundietf.de"
    api_url = host + "/mapi/ProductAPI/getProductsData"
    sitemap_url = host + "/sitemap/sitemap.xml"
    # the context the public German retail product pages embed in their HTML
    context = {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"}

    composition_fields = [
        "date",
        "type",
        "bbg",
        "isin",
        "name",
        "weight",
        "quantity",
        "currency",
        "sector",
        "country",
        "countryOfRisk",
    ]
    listing_characteristics = [
        "ISIN",
        "SHARE_MARKETING_NAME",
        "FUND_FUND_NAME",
        "TICKER",
        "MNEMO",
        "TICKER_BLOOMBERG",
        "ASSET_CLASS",
        "FUND_REPLICATION_METHODOLOGY",
    ]
    listing_batch_size = 50

    holding_col_mapping = {
        "date": "as_of_date",
        "type": "security_type",
        "bbg": "bloomberg_ticker",
        "quantity": "amount",
        "countryOfRisk": "location",
    }

    # ---------------------------------------------------------------- API

    @classmethod
    def _query_products(
        cls,
        isins: List[str],
        characteristics: List[str],
        composition: bool = False,
    ) -> List[dict]:
        body = {
            "context": cls.context,
            "productIds": list(isins),
            "productType": "PRODUCT",
            "characteristics": characteristics,
        }
        if composition:
            body["composition"] = {"compositionFields": cls.composition_fields}
        resp = cls.session.post(cls.api_url, json=body, expect=("json",))
        return resp.json().get("products") or []

    # ---------------------------------------------------------------- holdings

    @classmethod
    def _parse_holdings_resp(cls, product: dict) -> pd.DataFrame:
        """Parse one product of a getProductsData response into a holdings frame."""
        comp = product.get("composition") or {}
        rows = [
            r.get("compositionCharacteristics") or {}
            for r in comp.get("compositionData") or []
        ]
        if not rows:
            raise InvalidParameterError(
                f"{cls.provider}: no holdings published for {product.get('productId')} "
                "(eg a newly launched fund, or an exchange traded commodity)"
            )

        df = pd.DataFrame(rows).rename(columns=cls.holding_col_mapping)
        df["weight"] = pd.to_numeric(df.get("weight"), errors="coerce") * 100
        df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce")
        df["as_of_date"] = pd.to_datetime(df["as_of_date"], errors="coerce")
        # "ASML NA" -> ticker ASML, Bloomberg exchange code NA
        bbg = df.get("bloomberg_ticker")
        if bbg is not None:
            parts = bbg.fillna("").str.split()
            df["ticker"] = parts.str[0].replace("", np.nan)
            df["exchange"] = parts.str[1].replace("", np.nan)
        if "country" in df and "location" in df:
            df["location"] = df["location"].fillna(df["country"])
            df = df.drop(columns="country")
        return df

    @classmethod
    def retrieve_holdings(cls, ticker: str, isin: str) -> pd.DataFrame:
        """Retrieve the latest holdings for an Amundi UCITS ETF share class."""
        logger.debug(f"Querying {cls.provider} for {ticker or isin} holdings")
        products = cls._query_products([isin], ["ISIN"], composition=True)
        product = next(
            (p for p in products if str(p.get("productId", "")).upper() == isin.upper()),
            None,
        )
        if product is None:
            raise InvalidParameterError(f"{cls.provider}: unknown ISIN {isin}")
        df = cls._parse_holdings_resp(product)
        df["fund_ticker"] = ticker
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
        return cls.retrieve_holdings(sec_listing.ticker, sec_listing.isin)

    # ---------------------------------------------------------------- listings

    @classmethod
    def parse_sitemap(cls, xml: str) -> pd.DataFrame:
        """Product pages (one per share class) from the sitemap.

        The sitemap lists the products under /professionell/ only; we keep the
        ISIN and store the URL of the public retail page instead.
        """
        rows: Dict[str, dict] = {}
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            m = PRODUCT_URL_RE.match(loc)
            if not m:
                continue
            isin = m["isin"].upper()
            if not is_valid_isin(isin) or isin in rows:
                continue
            rows[isin] = {
                "isin": isin,
                "slug": m["slug"],
                "asset_class": m["asset_class"],
                "product_url": loc.replace("/de/professionell/", "/de/privatanleger/"),
            }
        return pd.DataFrame(
            list(rows.values()), columns=["isin", "slug", "asset_class", "product_url"]
        )

    @staticmethod
    def _first(chars: dict, *keys) -> Optional[str]:
        for k in keys:
            v = chars.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    @classmethod
    def parse_listing_products(cls, products: List[dict]) -> pd.DataFrame:
        rows = []
        for p in products:
            chars = p.get("characteristics") or {}
            bbg = cls._first(chars, "TICKER_BLOOMBERG")
            ticker = cls._first(chars, "TICKER", "MNEMO")
            if ticker is None and bbg:
                ticker = bbg.split()[0]
            rows.append(
                {
                    "isin": str(p.get("productId") or chars.get("ISIN") or "").upper(),
                    "ticker": ticker,
                    "fund_name": cls._first(chars, "SHARE_MARKETING_NAME", "FUND_FUND_NAME"),
                    "product_id": bbg,
                    "replication": cls._first(chars, "FUND_REPLICATION_METHODOLOGY"),
                    "api_asset_class": cls._first(chars, "ASSET_CLASS"),
                }
            )
        return pd.DataFrame(
            rows,
            columns=["isin", "ticker", "fund_name", "product_id", "replication", "api_asset_class"],
        )

    @classmethod
    def retrieve_listings(cls, max_funds: Optional[int] = None) -> pd.DataFrame:
        """All Amundi UCITS ETF share classes on the German site.

        One sitemap request, then one API request per listing_batch_size funds.
        """
        # the sitemap answers 404 to the session's default Accept: application/json
        sitemap = cls.session.get(
            cls.sitemap_url,
            expect=("xml",),
            headers={"Accept": "application/xml, text/xml;q=0.9, */*;q=0.8"},
        )
        pages = cls.parse_sitemap(sitemap.text)
        if max_funds:
            pages = pages.head(max_funds)

        frames = []
        isins = pages["isin"].tolist()
        for i in range(0, len(isins), cls.listing_batch_size):
            batch = isins[i : i + cls.listing_batch_size]
            frames.append(
                cls.parse_listing_products(
                    cls._query_products(batch, cls.listing_characteristics)
                )
            )
        details = pd.concat(frames, ignore_index=True) if frames else cls.parse_listing_products([])
        details = details.drop_duplicates("isin")

        listings = pages.merge(details, on="isin", how="left")
        slug_name = listings["slug"].str.replace("-", " ").str.title()
        listings["fund_name"] = listings["fund_name"].fillna(slug_name)
        listings["asset_class"] = listings["api_asset_class"].fillna(
            listings["asset_class"].str.replace("-", " ").str.title()
        )
        listings["domicile"] = listings["isin"].str[:2]
        listings["fund_type"] = np.where(
            listings["fund_name"].str.contains(r"\bETC\b|\bETN\b", na=False), "ETC", "ETF"
        )
        listings["provider"] = cls.provider
        cols = [
            "ticker",
            "fund_name",
            "isin",
            "product_id",
            "asset_class",
            "domicile",
            "product_url",
            "fund_type",
            "provider",
        ]
        return listings[cols].reset_index(drop=True)
