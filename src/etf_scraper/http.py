"""Shared HTTP plumbing for provider scrapers.

Every provider scraper should send its requests through a ProviderSession:
- identifies the tool honestly in the User-Agent (no browser imitation)
- applies a timeout to every request
- retries connection errors and 5xx/429 responses with exponential backoff
- rate limits requests per host
- raises UnexpectedResponseError when a provider returns an HTML page where data
  was expected, which is how most provider changes show up
"""

import logging
import threading
import time
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

try:
    from importlib.metadata import version as _pkg_version

    _VERSION = _pkg_version("etf_scraper")
except Exception:  # not installed, eg running from a source checkout
    _VERSION = "dev"

USER_AGENT = f"etf-scraper/{_VERSION} (+https://github.com/sirop/ETF-Scraper)"
DEFAULT_TIMEOUT = 60  # seconds
DEFAULT_MIN_INTERVAL = 1.0  # seconds between requests to the same host
RETRY_STATUS = {429, 500, 502, 503, 504}


class UnexpectedResponseError(Exception):
    """A provider returned something other than the expected data, typically an
    HTML page (a redirect, an error page or a disclaimer) instead of CSV/JSON/XLSX.
    """


class _RetryableHTTPError(requests.HTTPError):
    pass


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(
        exc, (requests.ConnectionError, requests.Timeout, _RetryableHTTPError)
    )


class _HostRateLimiter:
    """Enforce a minimum interval between requests to the same host (thread safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: Dict[str, float] = {}

    def wait(self, host: str, min_interval: float):
        with self._lock:
            now = time.monotonic()
            wait = self._last.get(host, 0.0) + min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()


_rate_limiter = _HostRateLimiter()


# content types that indicate an HTML page rather than data
_HTML_TYPES = ("text/html", "application/xhtml")


class ProviderSession:
    """A requests.Session wrapper for one provider.

    Args:
    - provider: provider name, used in log and error messages
    - headers: extra headers (eg Accept, Referer). User-Agent defaults to USER_AGENT.
    - min_interval: minimum seconds between requests to the same host
    - timeout: per request timeout in seconds
    - max_attempts: total attempts for retryable failures
    """

    def __init__(
        self,
        provider: str,
        headers: Optional[Dict[str, str]] = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = 4,
    ):
        self.provider = provider
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        if headers:
            self.session.headers.update(headers)

    def get(
        self,
        url: str,
        expect: Optional[Iterable[str]] = None,
        **kwargs,
    ) -> requests.Response:
        """GET url and return the response.

        Args:
        - expect: optional content type fragments the response must match, eg
        ("json",) or ("csv", "excel", "spreadsheet"). If the response is HTML, or
        matches none of them, raises UnexpectedResponseError.
        - kwargs: passed to requests (params, headers, ...)

        Raises:
        - requests.HTTPError for 4xx responses and 5xx after retries
        - UnexpectedResponseError if the content type doesn't match `expect`
        """
        kwargs.setdefault("timeout", self.timeout)
        host = urlparse(url).netloc

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            reraise=True,
        )
        def _get():
            _rate_limiter.wait(host, self.min_interval)
            resp = self.session.get(url, **kwargs)
            if resp.status_code in RETRY_STATUS:
                logger.warning(
                    f"{self.provider}: HTTP {resp.status_code} from {url}, retrying"
                )
                raise _RetryableHTTPError(
                    f"{resp.status_code} for {resp.url}", response=resp
                )
            return resp

        resp = _get()
        resp.raise_for_status()

        if expect is not None:
            check_content_type(resp, expect, self.provider)
        return resp


def check_content_type(
    resp: requests.Response, expect: Iterable[str], provider: str = ""
) -> None:
    """Raise UnexpectedResponseError unless resp's content type contains one of the
    expected fragments. HTML is always rejected when data is expected.
    """
    content_type = (resp.headers.get("content-type") or "").lower()
    body_start = resp.content[:200].lstrip().lower()
    looks_html = content_type.startswith(_HTML_TYPES) or body_start.startswith(
        (b"<!doctype html", b"<html")
    )
    expect = [e.lower() for e in expect]
    expected_desc = "/".join(expect) or "data"

    if looks_html and not any("html" in e for e in expect):
        raise UnexpectedResponseError(
            f"{provider}: expected {expected_desc} from {resp.url} but got an HTML "
            f"page (content-type {content_type!r}). The provider has probably changed "
            "its site or requires interactive access."
        )
    if expect and not any(e in content_type for e in expect):
        raise UnexpectedResponseError(
            f"{provider}: expected {expected_desc} from {resp.url} but got "
            f"content-type {content_type!r}"
        )
