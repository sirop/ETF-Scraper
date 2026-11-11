import sys

if sys.version_info[0] < 3:
    raise RuntimeError("Must be using Python 3")


try:
    # see https://setuptools.pypa.io/en/latest/userguide/datafiles.html
    from importlib.resources import files

    LISTINGS_FILE = files("etf_scraper.data").joinpath("listings.csv")
    HSBC_SEED_FILE = files("etf_scraper.data").joinpath("hsbc_etfs.csv")
    VANECK_EU_SEED_FILE = files("etf_scraper.data").joinpath("vaneck_eu_etfs.csv")
    WISDOMTREE_FUNDS_FILE = files("etf_scraper.data").joinpath("wisdomtree_funds.csv")
except:
    from pathlib import Path

    LISTINGS_FILE = Path(__file__).parent.joinpath("data/listings.csv")
    HSBC_SEED_FILE = Path(__file__).parent.joinpath("data/hsbc_etfs.csv")
    VANECK_EU_SEED_FILE = Path(__file__).parent.joinpath("data/vaneck_eu_etfs.csv")
    WISDOMTREE_FUNDS_FILE = Path(__file__).parent.joinpath("data/wisdomtree_funds.csv")
