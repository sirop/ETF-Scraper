"""Add (or refresh) the Amundi rows in src/etf_scraper/data/listings.csv.

Requests: one sitemap download plus one getProductsData request per 50 funds,
at most one request per second, with the tool's honest User-Agent.

Usage (from the repository root):
    python scripts/build_amundi_listings.py
"""

from pathlib import Path

import pandas as pd

from etf_scraper.scrapers import AmundiListings

LISTINGS = Path(__file__).resolve().parents[1] / "src" / "etf_scraper" / "data" / "listings.csv"


def main():
    new = AmundiListings.retrieve_listings()
    print(f"Amundi: {len(new)} share classes, {new['ticker'].notna().sum()} with a ticker")
    print(new.head(10).to_string())

    current = pd.read_csv(LISTINGS, dtype=str, keep_default_na=False)
    cols = list(current.columns)
    new = new.reindex(columns=cols).astype(object).fillna("")
    kept = current[current["provider"] != AmundiListings.provider]
    merged = pd.concat([kept, new], ignore_index=True)
    with open(LISTINGS, "w", encoding="utf-8", newline="\n") as f:
        merged.to_csv(f, index=False, lineterminator="\n")
    print(f"listings.csv: {len(current)} -> {len(merged)} rows")


if __name__ == "__main__":
    main()
