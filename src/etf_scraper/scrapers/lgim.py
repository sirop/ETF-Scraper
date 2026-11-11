"""L&G (Legal & General) UCITS ETFs, via fundcentres.landg.com.

Sources (plain GET requests with an honest User-Agent):
- Fund list (always): the German private investor sitemap lists the ETF fund pages;
  each page embeds its data as JSON ("static-data": fund name, asset class, share
  class ISIN, listings with tickers per exchange).
- Holdings (opt-in only, see etf_scraper.optin): each fund page links a
  "Download full fund holdings" CSV, the fund's daily basket (description, ISIN,
  trading currency, weight per constituent). It sits under /srp/documents-id/,
  which robots.txt disallows, so it is only downloaded when the user sets
  ETF_SCRAPER_OPT_IN=LGIM, for single fund queries, at most once per fund per day.
  L&G's terms of use allow downloading extracts for personal use; commercial use
  needs a licence from L&G.

The "Top 10" on the fund pages are the index's top constituents at the quarter
end, not the fund's holdings, so they are not used. Only the latest holdings are
available.
"""

import csv
import html
import json
import logging
import re
from datetime import date
from io import StringIO
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
from etf_scraper.optin import OPT_IN_ENV, cached_daily_download, opted_in
from etf_scraper.scrapers._common import _check_exp_provider

logger = logging.getLogger(__name__)

HOLDING_COL_MAPPING = {
    "Security Description": "name",
    "ISIN": "isin",
    "Trading Currency": "currency",
    "Constituent Weight (Base)": "weight",  # a fraction, converted to percent
}
CSV_HEADER = "Security Description"
FUND_PAGE_RE = re.compile(r"/fund-centre/ETF/[^/]+/([A-Z]{2}[A-Z0-9]{9}[0-9])/?$")
CSV_LINK_RE = re.compile(r'href="([^"]+/srp/documents-id/[^"]+/Fundholdings\.csv)"')
PREFERRED_EXCHANGE = "Deutsche Börse Xetra"


class LGIMListings(ProviderListings):
    provider = Provider.LGIM.value
    session = ProviderSession(Provider.LGIM.value, min_interval=3.0)

    host = "https://fundcentres.landg.com"
    sitemap_url = host + "/en/de/private-investors/sitemap.xml"

    # ---------------------------------------------------------------- holdings

    @classmethod
    def _parse_holdings_resp(cls, text: str) -> pd.DataFrame:
        """Parse a Fundholdings.csv file: the simple basket file most funds have, or
        a full portfolio composition file (eg synthetic commodity funds)."""
        if "PORTFOLIO COMPOSITION FILE" in text[:200]:
            return cls._parse_pcf(text)
        return cls._parse_basket(text)

    @staticmethod
    def _num(value) -> float:
        try:
            return float(str(value).replace(",", "").strip())
        except ValueError:
            return np.nan

    @classmethod
    def _parse_pcf(cls, text: str) -> pd.DataFrame:
        """Parse a portfolio composition file.

        Quantities are per creation basket. Weights are each position's value
        (quantity x price x contract size) relative to the basket NAV (NAV per share
        x shares per basket); amount and market value are scaled to the fund with
        the number of baskets outstanding.
        """
        rows = list(csv.reader(StringIO(text.lstrip("﻿"))))
        meta = {}
        for r in rows:
            if len(r) >= 2 and r[0].strip() and r[0].strip() not in meta:
                meta[r[0].strip()] = r[1].strip()
        base_ccy = meta.get("Fund Base Currency") or meta.get("Share Class Currency")
        basket_nav = cls._num(meta.get("Total NAV per share")) * cls._num(
            meta.get("Shares per Basket")
        )
        baskets = cls._num(meta.get("Baskets Outstanding"))

        records = []
        for i, r in enumerate(rows):
            section = r[0].strip() if r else ""
            if section not in ("Holdings:", "Holdings: Synthetics") or i + 1 >= len(rows):
                continue
            header = [h.strip() for h in rows[i + 1]]
            for line in rows[i + 2:]:
                if not any(c.strip() for c in line):
                    break
                d = dict(zip(header, (c.strip() for c in line)))
                price = cls._num(d.get("LOCAL PRICE") or d.get("PRICING LOCAL PRICE"))
                size = cls._num(d.get("CONTRACT SIZE")) if d.get("CONTRACT SIZE") else 1.0
                mult = cls._num(d.get("PRICE MULTIPLIER")) if d.get("PRICE MULTIPLIER") else 1.0
                qty = cls._num(d.get("PRICING BASKET QTY"))
                value = qty * price * size * mult
                currency = d.get("LOCAL CURRENCY") or None
                if currency and base_ccy and currency != base_ccy:
                    # the FX convention isn't confirmed yet, so don't guess
                    logger.warning(
                        f"{cls.provider}: {d.get('NAME')} is in {currency}, not {base_ccy}; "
                        "market value and weight left empty"
                    )
                    value = np.nan
                records.append({
                    "name": d.get("NAME") or None,
                    "isin": d.get("ISIN") or None,
                    "cusip": d.get("CUSIP") or None,
                    "sedol": d.get("SEDOL") or None,
                    "ticker": d.get("BLOOMBERG TICKER") or None,
                    "security_type": d.get("ASSET CLASS") or None,
                    "currency": currency,
                    "amount": qty * baskets,
                    "market_value": value * baskets,
                    "weight": 100 * value / basket_nav,
                    "price": price,
                })
        if not records:
            raise InvalidParameterError(f"{cls.provider}: no holdings in the composition file")
        df = pd.DataFrame(records)
        df.loc[~df["isin"].map(lambda v: isinstance(v, str) and is_valid_isin(v)), "isin"] = np.nan
        as_of = pd.to_datetime(meta.get("Trade Date"), format="%d/%m/%Y", errors="coerce")
        df["as_of_date"] = as_of if not pd.isna(as_of) else None
        df["fund_name"] = meta.get("Share Class Name")
        df["basket_isin"] = meta.get("Share Class ISIN")
        return df

    @classmethod
    def _parse_basket(cls, text: str) -> pd.DataFrame:
        """Parse the simple basket file (description, ISIN, currency, weight)."""
        lines = text.lstrip("﻿").splitlines()
        meta = {}
        header_row = None
        for i, line in enumerate(lines):
            if line.startswith(CSV_HEADER):
                header_row = i
                break
            for label in ("Basket Name", "ETF Trading ID", "Basket Trade Date"):
                if line.startswith(label):
                    meta[label] = line[len(label):].strip().strip(",")
        if header_row is None:
            raise InvalidParameterError(f"{cls.provider}: no holdings table in the file")

        table = []
        for line in lines[header_row:]:
            if not line.strip():
                break  # a blank line ends the table, notes and disclaimer follow
            table.append(line)
        df = pd.read_csv(StringIO("\n".join(table)), dtype=str, keep_default_na=False)
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns=HOLDING_COL_MAPPING)
        if df.empty:
            raise InvalidParameterError(f"{cls.provider}: no holdings published")

        df["weight"] = pd.to_numeric(df["weight"], errors="coerce") * 100
        for col in ["name", "isin", "currency"]:
            text_col = df[col].str.strip()
            df[col] = text_col.where(text_col != "", np.nan)
        df.loc[~df["isin"].map(lambda v: isinstance(v, str) and is_valid_isin(v)), "isin"] = np.nan

        as_of = pd.to_datetime(meta.get("Basket Trade Date"), errors="coerce")
        df["as_of_date"] = as_of if not pd.isna(as_of) else None
        df["fund_name"] = meta.get("Basket Name")
        df["basket_isin"] = meta.get("ETF Trading ID")
        return df

    @classmethod
    def _download_holdings_csv(cls, product_url: str) -> bytes:
        page = cls.session.get(product_url, expect=("html",))
        link = CSV_LINK_RE.search(page.text)
        if not link:
            raise InvalidParameterError(
                f"{cls.provider}: no full holdings download on {product_url}"
            )
        return cls.session.get(link[1], expect=("csv", "text", "octet-stream")).content

    @classmethod
    def retrieve_holdings(cls, isin: str, product_url: str, ticker: str = "") -> pd.DataFrame:
        """Latest holdings (the fund's daily basket). Needs the opt-in, see module docstring."""
        if not opted_in(cls.provider):
            raise InvalidParameterError(
                f"{cls.provider} full holdings are only available as an opt-in download "
                f"(robots.txt disallows the file; L&G's terms allow personal use). "
                f"Set {OPT_IN_ENV}={cls.provider} to enable it for single fund queries."
            )
        content = cached_daily_download(
            cls.provider, isin, lambda: cls._download_holdings_csv(product_url)
        )
        df = cls._parse_holdings_resp(content.decode("utf-8-sig", errors="replace"))
        if isinstance(df["basket_isin"].iloc[0], str) and df["basket_isin"].iloc[0] != isin:
            logger.warning(
                f"{cls.provider}: basket file is for {df['basket_isin'].iloc[0]}, queried {isin}"
            )
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
        if not isinstance(sec_listing.product_url, str) or not sec_listing.product_url:
            raise InvalidParameterError(f"No fund page known for {sec_listing.isin}")
        ticker = sec_listing.ticker if isinstance(sec_listing.ticker, str) else ""
        return cls.retrieve_holdings(sec_listing.isin, sec_listing.product_url, ticker)

    # ---------------------------------------------------------------- listings

    @staticmethod
    def parse_fund_page(html: str) -> Optional[dict]:
        """Fund details from the page's embedded JSON, or None if missing."""
        m = re.search(r'<script[^>]*id="static-data"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return None
        try:
            data = json.loads(m[1])
        except ValueError:
            return None
        fund = ((data.get("activeFund") or {}).get("data") or {}).get("data") or {}
        share_class = ((data.get("activeShareClass") or {}).get("data") or {}).get("data") or {}
        isin = share_class.get("shareclassISIN")
        if not isinstance(isin, str) or not is_valid_isin(isin):
            return None

        listings = []
        for section in data.get("pageSections") or []:
            part = (((section or {}).get("canvas") or {}).get("part") or {}).get("listings")
            if isinstance(part, dict):
                for rows in ((part.get("part") or {}).get("data") or {}).values():
                    listings += [r for r in rows if isinstance(r, dict) and r.get("isin") == isin]
        preferred = [r for r in listings if r.get("exchange") == PREFERRED_EXCHANGE]
        listing = (preferred or listings or [{}])[0]
        return {
            "fund_name": fund.get("name"),
            "asset_class": fund.get("assetClass"),
            "isin": isin,
            "ticker": listing.get("ticker"),
            "exchange": listing.get("exchange"),
            "currency": share_class.get("shareclassCurrency"),
        }

    @classmethod
    def list_fund_pages(cls) -> list:
        """ETF fund page URLs (with an ISIN) from the German private investor sitemap."""
        xml = cls.session.get(cls.sitemap_url, expect=("xml",)).text
        return cls.parse_sitemap(xml)

    @staticmethod
    def parse_sitemap(xml: str) -> list:
        # sitemap URLs are XML escaped, eg "Healthcare-Technology-&amp;-Innovation"
        urls = [html.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)]
        return sorted({u for u in urls if FUND_PAGE_RE.search(u)})

    @classmethod
    def retrieve_listings(
        cls, progress: Optional[Callable[[int, int, str], None]] = None
    ) -> pd.DataFrame:
        """Read every ETF fund page (one GET each, 3 s apart: a few minutes)."""
        urls = cls.list_fund_pages()
        rows = []
        for i, url in enumerate(urls, 1):
            if progress:
                progress(i, len(urls), url)
            try:
                page = cls.session.get(url, expect=("html",))
            except Exception as e:
                logger.warning(f"{cls.provider}: skipping {url}: {e}")
                continue
            info = cls.parse_fund_page(page.text)
            if info is None:
                logger.warning(f"{cls.provider}: no fund data on {url}, skipped")
                continue
            rows.append({
                **info,
                "product_url": url,
                "fund_type": "ETF",
                "provider": cls.provider,
                "domicile": info["isin"][:2],
            })
        cols = ["ticker", "fund_name", "isin", "asset_class", "exchange", "product_url",
                "fund_type", "provider", "domicile"]
        return pd.DataFrame(rows, columns=cols).drop_duplicates("isin").reset_index(drop=True)
