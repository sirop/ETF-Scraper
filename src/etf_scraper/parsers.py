"""Reusable parsing helpers for provider holdings files.

Provider downloads often have metadata lines above the table ("Fund Holdings as
of, ..."), thousands separators, placeholder values like "-", and dates in
formats such as "06/Nov/2026" or "14/Sept/2032".
"""

import re
from datetime import date, datetime
from io import StringIO
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# values providers use for "no value"
NA_VALUES = ["-", "--", "", "N/A", "n/a", "NA", "None"]

_MONTH_FIXES = {"sept": "sep", "june": "jun", "july": "jul", "mrz": "mar", "mai": "may",
                "okt": "oct", "dez": "dec", "mär": "mar"}


def split_preamble(
    text: str, header_cols: Sequence[str]
) -> Tuple[Dict[str, str], str]:
    """Split a CSV with metadata lines above the table.

    Finds the header line (the first line containing all header_cols as CSV
    fields) and returns (metadata, table_text). metadata maps the first field of
    each preamble line to its second field, eg {"Fund Holdings as of": "06/Nov/2026"}.

    Raises ValueError if no header line is found.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        fields = [f.strip().strip('"').strip() for f in line.split(",")]
        if all(col in fields for col in header_cols):
            metadata = {}
            for pre in lines[:i]:
                parts = [p.strip().strip('"').strip() for p in pre.split(",", 1)]
                if len(parts) == 2 and parts[0]:
                    metadata[parts[0]] = parts[1]
            return metadata, "\n".join(lines[i:])
    raise ValueError(f"No header line with columns {list(header_cols)} found")


def read_table(table_text: str) -> pd.DataFrame:
    """Read a CSV table (header + rows) with provider placeholders as missing.

    Columns are kept as text; convert with to_number/parse_date afterwards, since
    providers mix numbers and placeholders in the same column.
    """
    df = pd.read_csv(
        StringIO(table_text),
        dtype=str,
        keep_default_na=False,
        skip_blank_lines=True,
    )
    df.columns = [c.strip() for c in df.columns]
    # mask rather than replace: replace() warns about dtype downcasting on pandas 2.x
    df = df.mask(df.isin(NA_VALUES))
    # drop rows that are entirely empty (trailing blank lines, separators)
    return df.dropna(how="all").reset_index(drop=True)


def to_number(values: Iterable, decimal: str = ".", thousands: Optional[str] = ",") -> pd.Series:
    """Convert text numbers like "1,234.56", "1.234,56" or "7.5%" to floats (NaN if unparseable)."""
    s = pd.Series(values, dtype="object")
    mask = s.notna()
    t = s[mask].astype(str).str.strip().str.rstrip("%").str.strip()
    if thousands:
        t = t.str.replace(thousands, "", regex=False)
    if decimal != ".":
        t = t.str.replace(decimal, ".", regex=False)
    out = pd.Series(np.nan, index=s.index, dtype="float64")
    out[mask] = pd.to_numeric(t, errors="coerce")
    return out


def parse_date(value, formats: Sequence[str] = ("%d/%b/%Y", "%b %d, %Y", "%d.%b.%Y", "%d/%m/%Y", "%Y-%m-%d")) -> Optional[date]:
    """Parse a provider date string, tolerating month spellings like "Sept".

    Returns None if the value is missing or matches none of the formats.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip().strip('"')
    if not text or text in NA_VALUES:
        return None

    def fix_month(m):
        word = m.group(0)
        return _MONTH_FIXES.get(word.lower(), word)

    text = re.sub(r"[A-Za-zä]+", fix_month, text)
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# latest date pandas 2.x can hold (nanosecond resolution). Later dates do occur, eg
# 1000-year hybrid bonds (Orsted matures in 3024); they become missing on every
# pandas version so results don't depend on the version installed
_MAX_DATE = date(2262, 4, 11)


def parse_dates(values: Iterable, **kwargs) -> pd.Series:
    """Vectorised parse_date returning a datetime64 series (NaT if unparseable or
    beyond the pandas 2.x date range)."""
    parsed = [parse_date(v, **kwargs) for v in values]
    parsed = [d if d is None or d <= _MAX_DATE else None for d in parsed]
    return pd.to_datetime(pd.Series(parsed, dtype="object"))
