"""Common holdings format shared by all scrapers.

normalize_holdings() turns a scraper's raw holdings dataframe into the common layout:
the standard columns below come first in a fixed order with consistent dtypes, and
any provider specific columns are kept after them.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# fund level columns, identical for every row
FUND_COLUMNS = ["fund_ticker", "fund_isin", "provider", "as_of_date", "source"]

# holding level columns
HOLDING_COLUMNS = [
    "name",
    "ticker",
    "isin",
    "cusip",
    "sedol",
    "amount",
    "market_value",
    "weight",  # percent of the fund, eg 7.5 for 7.5%
    "currency",
    "security_type",
    "sector",
    "location",
    "exchange",
]

HOLDINGS_COLUMNS = FUND_COLUMNS + HOLDING_COLUMNS

NUMERIC_COLUMNS = ["amount", "market_value", "weight"]
STRING_COLUMNS = [
    c for c in HOLDINGS_COLUMNS if c not in NUMERIC_COLUMNS and c != "as_of_date"
]

# where the data came from
SOURCE_PROVIDER = "provider"  # the fund provider's own website/API
SOURCE_SEC_NPORT = "sec_nport"  # SEC EDGAR N-PORT filing

# sanity check bounds for the sum of weights (percent)
WEIGHT_SUM_BOUNDS = (90.0, 110.0)


def normalize_holdings(
    holdings_df: pd.DataFrame,
    fund_ticker: Optional[str] = None,
    fund_isin: Optional[str] = None,
    provider: Optional[str] = None,
    source: str = SOURCE_PROVIDER,
    check: bool = True,
) -> pd.DataFrame:
    """Return holdings in the common layout.

    Fund level values passed here override what's in the dataframe. Missing standard
    columns are added as empty, extra columns are kept after the standard ones.
    Doesn't modify the input.

    Args:
    - check: log warnings for suspicious data (see check_holdings)
    """
    df = holdings_df.copy()

    for col, value in [
        ("fund_ticker", fund_ticker),
        ("fund_isin", fund_isin),
        ("provider", provider),
        ("source", source),
    ]:
        if value is not None and not (isinstance(value, float) and np.isnan(value)):
            df[col] = value

    for col in HOLDINGS_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

    # one datetime resolution for all sources, so frames concatenate cleanly
    df["as_of_date"] = (
        pd.to_datetime(df["as_of_date"]).dt.normalize().astype("datetime64[ns]")
    )

    for col in STRING_COLUMNS:
        # keep missing values missing, strip whitespace from the rest
        s = df[col].astype("object")
        mask = s.notna()
        s[mask] = s[mask].astype(str).str.strip()
        s[mask & (s == "")] = np.nan
        df[col] = s

    extra = [c for c in df.columns if c not in HOLDINGS_COLUMNS]
    df = df[HOLDINGS_COLUMNS + extra].reset_index(drop=True)

    if check:
        check_holdings(df)
    return df


def check_holdings(df: pd.DataFrame) -> list:
    """Log and return warnings for suspicious holdings data:
    - more than one as_of_date
    - sum of weights outside WEIGHT_SUM_BOUNDS
    - fully duplicated rows
    """
    label = df["fund_ticker"].iloc[0] if len(df) and "fund_ticker" in df else "?"
    warnings = []

    if df.empty:
        warnings.append("no holdings")
    else:
        dates = df["as_of_date"].dropna().unique()
        if len(dates) != 1:
            warnings.append(f"expected one as_of_date, found {len(dates)}")

        if df["weight"].notna().any():
            total = df["weight"].sum()
            lo, hi = WEIGHT_SUM_BOUNDS
            if not lo <= total <= hi:
                warnings.append(
                    f"weights sum to {total:.2f}%, outside {lo:.0f}-{hi:.0f}% "
                    "(rounded weights or a parsing problem)"
                )

        n_dupl = int(df.duplicated().sum())
        if n_dupl:
            warnings.append(f"{n_dupl} fully duplicated rows")

    for w in warnings:
        logger.warning(f"Holdings check for {label}: {w}")
    return warnings
