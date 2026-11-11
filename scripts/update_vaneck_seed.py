"""Add VanEck UCITS fund page slugs to src/etf_scraper/data/vaneck_eu_etfs.csv.

VanEck's fund list is only available to browsers, so take it from a browser:
1. Open https://www.vaneck.com/ie/en/fundlisting/overview/etfs/ (answer the
   investor question truthfully) and wait until the fund table has loaded.
2. Either save the page (Ctrl+S, "Webpage, Complete"), or paste this into the
   browser console and save the copied text to a file:
   copy([...new Set([...document.querySelectorAll('a[href*="/investments/"]')].map(a => a.href))].join("\n"))
3. python scripts/update_vaneck_seed.py <saved file> [<more files>]
4. python scripts/build_listings.py VanEck_EU   (one request per fund, 25 s apart)

No requests are made by this script.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

from etf_scraper.scrapers import VanEckEUListings

SEED = Path(__file__).resolve().parents[1] / "src" / "etf_scraper" / "data" / "vaneck_eu_etfs.csv"


def main(paths):
    found = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        slugs = VanEckEUListings.slugs_from_html(text)
        print(f"{path}: {len(slugs)} fund page links")
        found += [s for s in slugs if s not in found]

    seed = pd.read_csv(SEED, dtype=str, keep_default_na=False)
    new = [s for s in found if s not in set(seed["slug"])]
    source = f"browser-{date.today():%Y-%m-%d}"
    seed = pd.concat([seed, pd.DataFrame({"slug": new, "source": source})], ignore_index=True)
    with open(SEED, "w", encoding="utf-8", newline="\n") as f:
        seed.to_csv(f, index=False, lineterminator="\n")
    print(f"{len(new)} new slugs added, {len(seed)} in {SEED.name}")
    if new:
        print("  " + ", ".join(new))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
