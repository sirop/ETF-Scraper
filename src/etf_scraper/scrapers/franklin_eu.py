"""Franklin Templeton UCITS ETFs, via franklintempleton.de.

Sources (plain requests with an honest User-Agent; robots.txt disallows nothing):
- Holdings: the product pages are a single page app that loads fund data with
  GraphQL queries, sent as POST /api/pds/price-and-performance. We send the same
  `Holdings` query the page sends (Portfolio -> fullholdings / dailyholdings):
  ticker, ISIN, SEDOL or CUSIP, name, quantity, weight, market value, currency
  and asset class per holding. Values are formatted for German readers
  ("10.500,00", dates "09.11.2026", "—" for missing).
- Fund list: the `PPSS` query the fund explorer sends (all ETF share classes with
  fund name, ticker, ISIN, Bloomberg ticker, SEDOL in one request), plus the
  product sitemap for each share class's page URL.

Holdings are per fund, so all share classes of a fund return the same holdings.
Only the latest holdings are available.
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
from etf_scraper.parsers import parse_dates, to_number
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)

# placeholders for missing values in Franklin's data
_MISSING = {"—", "-", "", "N/A"}

HOLDING_FIELDS = [
    "asofdate",
    "frequency",
    "secticker",
    "isinsecnbr",
    "cusipnbr",
    "secname",
    "quantityshrpar",
    "origcouponrate",
    "sctrname",
    "pctofnetassets",
    "mktvalue",
    "notionalmktvalue",
    "secexpdate",
    "assetclasscatg",
    "mktcurr",
    "contracts",
]

HOLDINGS_QUERY = """query Holdings($fundid: String!, $countrycode: String!, $languagecode: String!) {
  Portfolio(fundid: $fundid, countrycode: $countrycode, languagecode: $languagecode) {
    fundname
    producttype
    assetclass
    portfolio {
      dailyholdings { %(fields)s }
      fullholdings { %(fields)s }
    }
  }
}""" % {"fields": " ".join(HOLDING_FIELDS)}

LISTINGS_QUERY = """query IntlIndentifiers($countrycode: String!, $languagecode: String!, $productType: String!) {
  PPSS(countrycode: $countrycode, languagecode: $languagecode, productType: $productType) {
    fundid
    fundname
    assetclass
    shareclass {
      shclname
      identifiers { fundid shclcode ticker isin cusip bloomberg sedol wkn }
    }
  }
}"""

PRODUCT_URL_RE = re.compile(
    r"/produkt/(?P<fundid>\d+)/(?P<shclcode>[A-Za-z0-9]+)/[^/]+/(?P<isin>[A-Z]{2}[A-Z0-9]{9}[0-9])/?$"
)


# the German site returns asset classes in German; other providers use English
ASSET_CLASSES = {
    "Aktien": "Equity",
    "Anleihen": "Fixed Income",
    "Renten": "Fixed Income",
    "Festverzinsliche Wertpapiere": "Fixed Income",
    "Multi-Asset": "Multi Asset",
    "Mischfonds": "Multi Asset",
    "Geldmarkt": "Money Market",
    "Alternative": "Alternatives",
    "Alternativ": "Alternatives",
}


def _clean(values) -> pd.Series:
    s = pd.Series(values, dtype="object")
    return s.where(~s.isin(_MISSING) & s.notna(), np.nan)


class FranklinEUListings(ProviderListings):
    provider = Provider.Franklin_EU.value
    session = ProviderSession(
        Provider.Franklin_EU.value,
        headers={"Accept": "application/json"},
        min_interval=2.0,
    )

    host = "https://www.franklintempleton.de"
    api_url = host + "/api/pds/price-and-performance"
    sitemap_url = (
        host + "/binaries/content/assets/global/sitemaps/google/de-de_product.xml"
    )
    countrycode = "DE"
    languagecode = "de_DE"

    # ---------------------------------------------------------------- API

    @classmethod
    def _graphql(cls, operation: str, query: str, variables: Dict[str, str]) -> dict:
        body = {"operationName": operation, "query": query, "variables": variables}
        resp = cls.session.post(cls.api_url, json=body, expect=("json",))
        data = resp.json()
        if data.get("errors") and not data.get("data"):
            raise InvalidParameterError(f"{cls.provider}: {str(data['errors'])[:300]}")
        return data.get("data") or {}

    # ---------------------------------------------------------------- holdings

    @classmethod
    def _parse_holdings_resp(cls, data: dict) -> pd.DataFrame:
        """Parse the data of a Holdings response into a holdings frame."""
        portfolio = ((data.get("Portfolio") or {}).get("portfolio")) or {}
        # full holdings are month end and daily ones the latest; prefer the latest
        rows = portfolio.get("dailyholdings") or portfolio.get("fullholdings") or []
        if not rows:
            raise InvalidParameterError(f"{cls.provider}: no holdings published")

        raw = pd.DataFrame(rows)
        ids = _clean(raw.get("cusipnbr")).str.strip()
        # the column holds 7 character SEDOLs for most non US securities and
        # 9 character CUSIPs for US ones (other text, eg "INR CGT", is neither)
        df = pd.DataFrame(
            {
                "as_of_date": parse_dates(raw["asofdate"], formats=("%d.%m.%Y", "%m/%d/%Y")),
                "name": _clean(raw.get("secname")),
                "ticker": _clean(raw.get("secticker")),
                "isin": _clean(raw.get("isinsecnbr")),
                "cusip": ids.where(ids.str.fullmatch(r"[0-9A-Z]{8}[0-9]", na=False)),
                # SEDOLs use digits and consonants (no vowels)
                "sedol": ids.where(ids.str.fullmatch(r"[0-9B-DF-HJ-NP-TV-Z]{6}[0-9]", na=False)),
                "amount": to_number(_clean(raw.get("quantityshrpar")), decimal=",", thousands="."),
                "market_value": to_number(_clean(raw.get("mktvalue")), decimal=",", thousands="."),
                "weight": to_number(_clean(raw.get("pctofnetassets")), decimal=",", thousands="."),
                "currency": _clean(raw.get("mktcurr")),
                "security_type": _clean(raw.get("assetclasscatg")),
                "sector": _clean(raw.get("sctrname")),
                "notional_market_value": to_number(
                    _clean(raw.get("notionalmktvalue")), decimal=",", thousands="."
                ),
                "coupon": to_number(_clean(raw.get("origcouponrate")), decimal=",", thousands="."),
                "maturity_date": parse_dates(
                    _clean(raw.get("secexpdate")), formats=("%d.%m.%Y", "%m/%d/%Y")
                ),
                "holdings_frequency": _clean(raw.get("frequency")),
            }
        )
        return df

    @classmethod
    def retrieve_holdings(cls, ticker: str, fund_id: str) -> pd.DataFrame:
        """Retrieve the latest holdings of a fund by Franklin's fund id (eg "40163")."""
        logger.debug(f"Querying {cls.provider} for {ticker} holdings (fund {fund_id})")
        data = cls._graphql(
            "Holdings",
            HOLDINGS_QUERY,
            {"fundid": str(fund_id), "countrycode": cls.countrycode, "languagecode": cls.languagecode},
        )
        df = cls._parse_holdings_resp(data)
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
        # product_id is "<fund id>/<share class code>", eg "40163/ETA"
        fund_id = str(sec_listing.product_id or "").split("/")[0]
        if not fund_id.isdigit():
            raise InvalidParameterError(f"No Franklin fund id known for {sec_listing.ticker}")
        return cls.retrieve_holdings(sec_listing.ticker, fund_id)

    # ---------------------------------------------------------------- listings

    @staticmethod
    def _share_class_name(fund_name: Optional[str], class_name: Optional[str]) -> Optional[str]:
        """Single class funds repeat the fund name as the class name; for funds with
        several classes the class name is only a suffix, eg "(Dis)" or
        "EUR Hedged (Acc)", so prefix the fund name."""
        fund_name = (fund_name or "").strip()
        class_name = (class_name or "").strip()
        if not class_name or class_name in _MISSING:
            return fund_name or None
        if not fund_name or fund_name.lower() in class_name.lower():
            return class_name
        return f"{fund_name} {class_name}"

    @classmethod
    def parse_listings_resp(cls, data: dict) -> pd.DataFrame:
        rows = []
        for fund in data.get("PPSS") or []:
            for sc in fund.get("shareclass") or []:
                ids = sc.get("identifiers") or {}
                rows.append(
                    {
                        "fund_id": ids.get("fundid") or fund.get("fundid"),
                        "share_class": ids.get("shclcode"),
                        "ticker": ids.get("ticker"),
                        "isin": ids.get("isin"),
                        "cusip": ids.get("cusip"),
                        "bloomberg": ids.get("bloomberg"),
                        "fund_name": cls._share_class_name(
                            fund.get("fundname"), sc.get("shclname")
                        ),
                        "asset_class": fund.get("assetclass"),
                    }
                )
        cols = ["fund_id", "share_class", "ticker", "isin", "cusip", "bloomberg", "fund_name", "asset_class"]
        df = pd.DataFrame(rows, columns=cols)
        for c in cols:
            df[c] = _clean(df[c])
        df["isin"] = df["isin"].where(df["isin"].map(lambda x: isinstance(x, str) and is_valid_isin(x)))
        # a few share classes have a Bloomberg ticker but no plain ticker
        df["ticker"] = df["ticker"].fillna(df["bloomberg"].str.split().str[0])
        return df

    @classmethod
    def parse_sitemap(cls, xml: str) -> pd.DataFrame:
        rows = {}
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            m = PRODUCT_URL_RE.search(loc)
            if m and "/preise-etfs/" in loc:
                rows.setdefault(m["isin"], {"isin": m["isin"], "product_url": loc})
        return pd.DataFrame(list(rows.values()), columns=["isin", "product_url"])

    @classmethod
    def retrieve_listings(cls) -> pd.DataFrame:
        """All Franklin UCITS ETF share classes on the German site (two requests)."""
        data = cls._graphql(
            "IntlIndentifiers",
            LISTINGS_QUERY,
            {"countrycode": cls.countrycode, "languagecode": cls.languagecode, "productType": "etf"},
        )
        shares = cls.parse_listings_resp(data).dropna(subset=["isin"])
        sitemap = cls.session.get(
            cls.sitemap_url,
            expect=("xml",),
            headers={"Accept": "application/xml, text/xml;q=0.9, */*;q=0.8"},
        )
        pages = cls.parse_sitemap(sitemap.text)
        listings = shares.merge(pages, on="isin", how="left")

        listings["product_id"] = listings["fund_id"] + "/" + listings["share_class"].fillna("")
        listings["cusip"] = listings["cusip"].where(listings["cusip"].str.len() == 9)
        listings["exchange"] = listings["bloomberg"].str.split().str[-1]
        listings["asset_class"] = listings["asset_class"].replace(ASSET_CLASSES)
        listings["domicile"] = listings["isin"].str[:2]
        listings["fund_type"] = "ETF"
        listings["provider"] = cls.provider
        cols = [
            "ticker",
            "fund_name",
            "isin",
            "cusip",
            "product_id",
            "asset_class",
            "exchange",
            "domicile",
            "product_url",
            "fund_type",
            "provider",
        ]
        return listings[cols].drop_duplicates("isin").reset_index(drop=True)
