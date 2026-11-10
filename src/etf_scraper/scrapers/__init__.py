"""Provider scrapers, one module per provider.

Import scrapers from here, eg `from etf_scraper.scrapers import VanguardListings`.
"""

from etf_scraper.scrapers.ishares import ISharesListings
from etf_scraper.scrapers.ssga import SSGAListings
from etf_scraper.scrapers.vanguard import VanguardListings
from etf_scraper.scrapers.invesco import InvescoListings
from etf_scraper.scrapers.ishares_eu import ISharesEUListings
from etf_scraper.scrapers.ssga_eu import SSGAEUListings

__all__ = [
    "SSGAEUListings",
    "ISharesListings",
    "SSGAListings",
    "VanguardListings",
    "InvescoListings",
    "ISharesEUListings",
]
