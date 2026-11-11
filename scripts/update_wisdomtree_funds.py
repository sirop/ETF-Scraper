"""Add WisdomTree product numbers to src/etf_scraper/data/wisdomtree_funds.csv.

WisdomTree's holdings downloads are named "fund-holdings-<number>.csv" and don't
say which fund they belong to. Save the fund page in your browser (Ctrl+S) and run:

    python scripts/update_wisdomtree_funds.py <saved page.html> [<more pages>]

The product number, ISIN, ticker and name of the share classes on the page are
added. No requests are made.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

from etf_scraper.manual_import import load_wisdomtree_funds, wisdomtree_funds_from_page

FILE = Path(__file__).resolve().parents[1] / "src" / "etf_scraper" / "data" / "wisdomtree_funds.csv"


def main(paths):
    known = load_wisdomtree_funds()
    added = []
    for path in paths:
        found = wisdomtree_funds_from_page(Path(path).read_text(encoding="utf-8", errors="replace"))
        print(f"{path}: {len(found)} share classes")
        for row in found.to_dict("records"):
            if row["fund_id"] in set(known["fund_id"]):
                continue
            row["source"] = f"saved-page-{date.today():%Y-%m-%d}"
            known = pd.concat([known, pd.DataFrame([row])], ignore_index=True)
            added.append(row)
    known = known.sort_values("fund_id")
    with open(FILE, "w", encoding="utf-8", newline="\n") as f:
        known.to_csv(f, index=False, lineterminator="\n")
    for row in added:
        print(f"  added {row['fund_id']}: {row['isin']} {row['ticker']} {row['fund_name']}")
    print(f"{len(added)} added, {len(known)} in {FILE.name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
