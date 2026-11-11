"""Opt-in access to downloads that a provider's robots.txt disallows.

Some providers link a full holdings file on their public fund pages, but under a
path their robots.txt disallows. robots.txt is written for crawlers; a single
download a person asks for, at most once a day, for personal use (and only where
the provider's terms of use allow personal use) is a different case. Such
downloads are off by default and are enabled per provider by the user:

    ETF_SCRAPER_OPT_IN=LGIM          (comma separated provider names)

Rules enforced here and in the scrapers that use it:
- only for single fund holdings queries, never for listing builds or CI
- at most one download per fund per day: results are cached on disk
  (ETF_SCRAPER_CACHE, default ~/.cache/etf_scraper)
- the honest User-Agent, as for every other request
"""

import os
from datetime import date
from pathlib import Path
from typing import Callable, Optional

OPT_IN_ENV = "ETF_SCRAPER_OPT_IN"
CACHE_ENV = "ETF_SCRAPER_CACHE"


def opted_in(provider: str) -> bool:
    """True if the user enabled opt-in downloads for this provider."""
    names = os.getenv(OPT_IN_ENV, "")
    return provider.lower() in {n.strip().lower() for n in names.split(",") if n.strip()}


def cache_dir() -> Path:
    return Path(os.getenv(CACHE_ENV) or Path.home() / ".cache" / "etf_scraper")


def cached_daily_download(
    provider: str,
    key: str,
    download: Callable[[], bytes],
    suffix: str = ".csv",
    today: Optional[date] = None,
) -> bytes:
    """Return today's cached file for (provider, key), downloading it at most once
    a day. Older cache files for the same key are removed."""
    today = today or date.today()
    folder = cache_dir() / provider
    path = folder / f"{key}_{today:%Y%m%d}{suffix}"
    if path.exists():
        return path.read_bytes()
    content = download()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{key}_*{suffix}"):
        old.unlink()
    path.write_bytes(content)
    return content
