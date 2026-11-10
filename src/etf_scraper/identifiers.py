"""Security identifier helpers (ISIN, CUSIP).

An ISIN is a 2 letter country code, a 9 character national identifier and a check
digit. For US securities the national identifier is the CUSIP, so a US ISIN can be
derived exactly from its CUSIP.
"""

import re
from typing import Optional

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CUSIP_RE = re.compile(r"^[A-Z0-9]{8}[0-9]$")


def _digits(code: str) -> str:
    """Map letters to numbers (A=10 ... Z=35) as used by the ISIN check digit."""
    return "".join(str(int(c, 36)) for c in code)


def isin_check_digit(isin_without_check: str) -> str:
    """Compute the ISIN check digit (Luhn algorithm over the expanded digits)."""
    digits = _digits(isin_without_check.upper())
    total = 0
    # double every second digit, starting from the rightmost
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def is_valid_isin(value) -> bool:
    """True if value is a well formed ISIN with a correct check digit."""
    if not isinstance(value, str):
        return False
    value = value.strip().upper()
    if not _ISIN_RE.match(value):
        return False
    return isin_check_digit(value[:11]) == value[11]


def looks_like_isin(value) -> bool:
    """True if value has the shape of an ISIN (used to tell ISINs from tickers)."""
    return isinstance(value, str) and bool(_ISIN_RE.match(value.strip().upper()))


def isin_from_cusip(cusip, country: str = "US") -> Optional[str]:
    """Build an ISIN from a 9 character CUSIP, or None if the CUSIP is malformed."""
    if not isinstance(cusip, str):
        return None
    cusip = cusip.strip().upper()
    if not _CUSIP_RE.match(cusip):
        return None
    base = f"{country.upper()}{cusip}"
    return base + isin_check_digit(base)
