"""Add (or refresh) one provider's rows in src/etf_scraper/data/listings.csv.

Requests: whatever that provider's retrieve_listings needs (a few for most
providers), rate limited, with the tool's honest User-Agent.

Usage (from the repository root):
    python scripts/build_listings.py Amundi
    python scripts/build_listings.py Franklin_EU
"""

import sys
from pathlib import Path

import pandas as pd

from etf_scraper.api import SCRAPERS

LISTINGS = Path(__file__).resolve().parents[1] / "src" / "etf_scraper" / "data" / "listings.csv"


def main(provider: str):
    if provider not in SCRAPERS:
        sys.exit(f"Unknown provider {provider!r}, choose from: {', '.join(SCRAPERS)}")
    new = SCRAPERS[provider].retrieve_listings()
    print(f"{provider}: {len(new)} share classes, {new['ticker'].notna().sum()} with a ticker")
    print(new.head(10).to_string())

    current = pd.read_csv(LISTINGS, dtype=str, keep_default_na=False)
    cols = list(current.columns)
    new = new.reindex(columns=cols).astype(object).fillna("")
    kept = current[current["provider"] != provider]
    merged = pd.concat([kept, new], ignore_index=True)
    with open(LISTINGS, "w", encoding="utf-8", newline="\n") as f:
        merged.to_csv(f, index=False, lineterminator="\n")
    print(f"listings.csv: {len(current)} -> {len(merged)} rows")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
