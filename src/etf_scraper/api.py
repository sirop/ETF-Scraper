import logging
from functools import lru_cache
from datetime import date
from multiprocessing.pool import ThreadPool
from typing import List, Union

import pandas as pd

from etf_scraper.config import LISTINGS_FILE
from etf_scraper.base import Provider, SecurityListing
from etf_scraper.identifiers import looks_like_isin
from etf_scraper.scrapers import (
    ISharesListings,
    InvescoListings,
    VanguardListings,
    SSGAListings,
    ISharesEUListings,
    SSGAEUListings,
    InvescoEUListings,
    GlobalXEUListings,
    AmundiListings,
)

SCRAPERS = {
    k.provider: k
    for k in [
        ISharesListings,
        InvescoListings,
        VanguardListings,
        SSGAListings,
        ISharesEUListings,
        SSGAEUListings,
        InvescoEUListings,
        GlobalXEUListings,
        AmundiListings,
    ]
}

logger = logging.getLogger(__name__)


def query_listings(providers: Union[None, List[Provider]] = None) -> pd.DataFrame:
    """Query provider listings for the providers given (or all if none given)
    and return as a df
    """
    logger.debug("Querying for provider listings")

    if not providers:
        providers = list(SCRAPERS)
        logger.info(
            f"No providers given, defaulting to all providers: {', '.join(providers)}"
        )

    # TODO: add retry decorator + try/except block
    def query_listings_(provider: Provider) -> pd.DataFrame:
        logger.info(f"Querying for fund listings from {provider}")
        listings_df = SCRAPERS[provider].retrieve_listings()
        logger.info(f"Retrieved {len(listings_df)} listings from {provider}")
        return listings_df

    n_threads = len(
        providers
    )  # FIXME: don't see any problems but it doesn't seem sensible

    with ThreadPool(n_threads) as p:
        from_pool = p.map(query_listings_, providers)

    # TODO: check for duplicate tickers
    return pd.concat(from_pool)


@lru_cache
def load_listings() -> pd.DataFrame:
    """Load the listings file shipped with the etf_scraper module"""
    logging.debug(f"Loading default listings file from {LISTINGS_FILE}")
    # read identifiers as text so leading zeros survive (eg Vanguard product id 0968)
    id_cols = ["ticker", "cusip", "isin", "product_id", "exchange", "domicile"]
    return pd.read_csv(LISTINGS_FILE, dtype={c: "string" for c in id_cols})


class ETFScraper:
    def __init__(self, listings_df: Union[pd.DataFrame, None] = None):
        if listings_df is None:
            listings_df = load_listings()

        self.listings_df = listings_df.drop_duplicates()

    def _find_sec_listing(
        self,
        identifier: str,
        provider: Union[str, Provider, None] = None,
        exchange: Union[str, None] = None,
    ) -> SecurityListing:
        """Generate a SecurityListing for the given ticker or ISIN.

        Args:
        - identifier: a fund ticker (eg VOO) or ISIN (eg US9229083632)
        - provider: optionally restrict to one provider, eg to tell apart funds that
        share a ticker on different exchanges
        - exchange: optionally restrict to one listing exchange

        Raises:
        - ValueError: if there are multiple/no securities matching.
        """
        df = self.listings_df
        identifier = identifier.strip()

        if looks_like_isin(identifier) and "isin" in df:
            matches = df[df["isin"].str.upper() == identifier.upper()]
            id_type = "ISIN"
        else:
            matches = df[df["ticker"].str.upper() == identifier.upper()]
            id_type = "ticker"

        if provider is not None:
            provider = provider.value if isinstance(provider, Provider) else provider
            matches = matches[matches["provider"] == provider]
        if exchange is not None and "exchange" in df:
            matches = matches[matches["exchange"].str.upper() == exchange.upper()]

        cols = [c for c in ("ticker", "isin", "provider", "exchange", "fund_name") if c in matches]

        # Backwards compatibility: tickers used to mean US funds. If a bare ticker
        # matches several funds and exactly one is a US fund, use it. UCITS funds
        # sharing the ticker are reached by ISIN or provider.
        if (
            len(matches) > 1
            and id_type == "ticker"
            and provider is None
            and exchange is None
            and "domicile" in matches
        ):
            us_matches = matches[matches["domicile"] == "US"]
            if len(us_matches) == 1:
                others = matches.drop(us_matches.index)
                logger.info(
                    f"Ticker {identifier} matches several funds, using the US fund. "
                    f"Pass the ISIN or provider for the others:\n{others[cols].to_string()}"
                )
                matches = us_matches

        if len(matches) == 0:
            raise ValueError(f"No listings found for {id_type} {identifier}")
        elif len(matches) > 1:
            raise ValueError(
                f"Multiple listings found for {id_type} {identifier}, use the ISIN or "
                f"pass provider and/or exchange to choose one:\n{matches[cols].to_string()}"
            )

        sec_dict = matches.iloc[0].to_dict()
        fields = set(SecurityListing._fields)
        return SecurityListing(**{k: v for k, v in sec_dict.items() if k in fields})

    def query_holdings(
        self,
        ticker: str,
        holdings_date: Union[date, str, None] = None,
        provider: Union[str, Provider, None] = None,
        exchange: Union[str, None] = None,
    ) -> pd.DataFrame:
        """Query for holdings for the given fund + holdings_date.

        Args:
        - ticker: the fund ticker or ISIN to query. Must be in self.listings_df.
        - holdings date: the holdings date to query. If blank then will query for the latest
        possible holdings. If given as a string, then attempt to parse using
        pd.to_datetime.
        - provider, exchange: optional, to choose between funds sharing a ticker

        Returns holdings in the common format, see etf_scraper.holdings.
        """
        if holdings_date and isinstance(holdings_date, str):
            holdings_date = pd.to_datetime(holdings_date).date()

        if not ticker:
            raise ValueError("Need to pass a ticker or ISIN to scrape it")

        sec_listing = self._find_sec_listing(ticker, provider, exchange)

        date_log_str = "latest" if not holdings_date else holdings_date
        logger.debug(
            f"Querying for {sec_listing.ticker} holdings as of {date_log_str}"
            f" from {sec_listing.provider}"
        )
        scraper = SCRAPERS[sec_listing.provider]
        return scraper.query_holdings(sec_listing, holdings_date)
