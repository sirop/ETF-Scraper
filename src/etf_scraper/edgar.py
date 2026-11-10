"""Historical fund holdings from SEC EDGAR N-PORT filings.

US registered funds (ETFs and mutual funds) report their full portfolio to the SEC on
Form N-PORT. The public filings (NPORT-P) are free on EDGAR and go back to 2019, which
makes them a source of historical holdings where providers only publish the latest
holdings (eg Vanguard, Invesco).

Caveats:
- Filings are made public about 60 days after the reporting date, and historically
  only for one month per quarter.
- Holdings are reported per fund series, so all share classes of a fund (eg VOO and
  VFIAX) share the same holdings.
- Holdings are identified by name/CUSIP/ISIN. Tickers are only present when the filer
  includes them.
- Funds that aren't registered under the 1940 Act (eg unit investment trusts) don't
  file N-PORT.

The SEC requires automated clients to declare a contact in the User-Agent
(https://www.sec.gov/os/accessing-edgar-data), eg "Sample Company admin@example.com".
Set it with the SEC_USER_AGENT environment variable or pass user_agent explicitly.
"""

import logging
import os
import re
import time
import threading
import xml.etree.ElementTree as ET
from datetime import date
from functools import lru_cache
from typing import Dict, List, Optional, Union

import pandas as pd
import requests

from etf_scraper.base import InvalidParameterError
from etf_scraper.holdings import SOURCE_SEC_NPORT, normalize_holdings

logger = logging.getLogger(__name__)

SEC_USER_AGENT_ENV = "SEC_USER_AGENT"
MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
NPORT_FORM = "NPORT-P"
NPORT_NS = {"n": "http://www.sec.gov/edgar/nport"}

# SEC fair access policy allows at most 10 requests per second
_MIN_REQUEST_INTERVAL = 0.15
_rate_lock = threading.Lock()
_last_request = [0.0]


def _get_user_agent(user_agent: Optional[str] = None) -> str:
    ua = user_agent or os.getenv(SEC_USER_AGENT_ENV)
    if not ua:
        raise RuntimeError(
            f"SEC EDGAR requires a User-Agent with contact details. Set the "
            f"{SEC_USER_AGENT_ENV} environment variable, eg "
            f'"Your Name your.email@example.com".'
        )
    return ua


def _sec_get(url: str, user_agent: Optional[str] = None, **kwargs) -> requests.Response:
    """GET from sec.gov, respecting the SEC rate limit."""
    headers = {"User-Agent": _get_user_agent(user_agent)}
    with _rate_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
    resp = requests.get(url, headers=headers, timeout=60, **kwargs)
    if resp.status_code == 403:
        raise PermissionError(
            "SEC EDGAR refused the request (403). Check that the User-Agent contains a "
            "contact name and email, and that you're under 10 requests per second."
        )
    resp.raise_for_status()
    return resp


@lru_cache(maxsize=4)
def _load_mf_tickers(user_agent: str) -> pd.DataFrame:
    data = _sec_get(MF_TICKERS_URL, user_agent).json()
    return pd.DataFrame(data["data"], columns=data["fields"])


def lookup_fund(ticker: str, user_agent: Optional[str] = None) -> Dict[str, str]:
    """Map a fund ticker to its SEC identifiers: cik, series_id and class_id."""
    ua = _get_user_agent(user_agent)
    tickers = _load_mf_tickers(ua)
    match = tickers[tickers["symbol"].str.upper() == ticker.upper()]
    if match.empty:
        raise InvalidParameterError(
            f"Ticker {ticker} not found in the SEC fund ticker list. Only registered "
            "funds (not eg unit investment trusts) file N-PORT."
        )
    row = match.iloc[0]
    return {
        "ticker": ticker.upper(),
        "cik": str(row["cik"]),
        "series_id": row["seriesId"],
        "class_id": row["classId"],
    }


def _parse_period_of_report(headers_text: str) -> Optional[date]:
    match = re.search(r"PERIOD OF REPORT:\s*(\d{8})", headers_text)
    if not match:
        return None
    return pd.Timestamp(match.group(1)).date()


def list_nport_filings(
    ticker: str,
    max_filings: int = 40,
    user_agent: Optional[str] = None,
) -> pd.DataFrame:
    """List a fund's public N-PORT filings, newest first.

    Returns a df with columns: report_date, filing_date, accession_number, folder_url.
    Looks up each filing's reporting period, so this makes one request per filing.
    """
    fund = lookup_fund(ticker, user_agent)
    params = {
        "action": "getcompany",
        "CIK": fund["series_id"],
        "type": NPORT_FORM,
        "dateb": "",
        "owner": "include",
        "count": min(max_filings, 100),
        "output": "atom",
    }
    atom = _sec_get(BROWSE_EDGAR_URL, user_agent, params=params).text

    rows = []
    for entry in re.findall(r"<entry>(.*?)</entry>", atom, flags=re.S):
        form = re.search(r"<filing-type>(.*?)</filing-type>", entry)
        if not form or form.group(1) != NPORT_FORM:
            continue  # skip amendments (NPORT-P/A) and other forms
        href = re.search(r"<filing-href>(.*?)</filing-href>", entry).group(1)
        accession = re.search(r"<accession-number>(.*?)</accession-number>", entry).group(1)
        filed = re.search(r"<filing-date>(.*?)</filing-date>", entry).group(1)
        folder_url = href.rsplit("/", 1)[0] + "/"
        rows.append(
            {
                "filing_date": pd.Timestamp(filed).date(),
                "accession_number": accession,
                "folder_url": folder_url,
            }
        )

    for row in rows[:max_filings]:
        headers_url = f"{row['folder_url']}{row['accession_number']}-index-headers.html"
        row["report_date"] = _parse_period_of_report(
            _sec_get(headers_url, user_agent).text
        )

    filings = pd.DataFrame(
        rows[:max_filings],
        columns=["report_date", "filing_date", "accession_number", "folder_url"],
    )
    return filings


def _text(elem: Optional[ET.Element], path: str) -> Optional[str]:
    if elem is None:
        return None
    found = elem.find(path, NPORT_NS)
    if found is None:
        return None
    return found.text.strip() if found.text else None


def _attr(elem: ET.Element, path: str, attr: str = "value") -> Optional[str]:
    found = elem.find(path, NPORT_NS)
    return found.get(attr) if found is not None else None


def parse_nport_xml(xml_content: Union[bytes, str]) -> pd.DataFrame:
    """Parse an N-PORT primary_doc.xml into a holdings df.

    Columns follow etf_scraper's holdings format where possible, plus N-PORT specific
    fields (asset_category, issuer_category, payoff_profile, units, series_id,
    net_assets).
    """
    root = ET.fromstring(xml_content)
    form = root.find("n:formData", NPORT_NS)
    gen_info = form.find("n:genInfo", NPORT_NS)
    fund_info = form.find("n:fundInfo", NPORT_NS)

    report_date = _text(gen_info, "n:repPdDate")
    series_id = _text(gen_info, "n:seriesId")
    series_name = _text(gen_info, "n:seriesName")
    net_assets = _text(fund_info, "n:netAssets")

    records: List[dict] = []
    for sec in form.iterfind("n:invstOrSecs/n:invstOrSec", NPORT_NS):
        records.append(
            {
                "name": _text(sec, "n:name"),
                "title": _text(sec, "n:title"),
                "ticker": _attr(sec, "n:identifiers/n:ticker"),
                "cusip": _text(sec, "n:cusip"),
                "isin": _attr(sec, "n:identifiers/n:isin"),
                "lei": _text(sec, "n:lei"),
                "amount": _text(sec, "n:balance"),
                "units": _text(sec, "n:units"),
                "currency": _text(sec, "n:curCd"),
                "market_value": _text(sec, "n:valUSD"),
                "weight": _text(sec, "n:pctVal"),
                "payoff_profile": _text(sec, "n:payoffProfile"),
                "asset_category": _text(sec, "n:assetCat"),
                "issuer_category": _text(sec, "n:issuerCat"),
                "location": _text(sec, "n:invCountry"),
            }
        )

    holdings_df = pd.DataFrame(records)
    for col in ["amount", "market_value", "weight"]:
        holdings_df[col] = pd.to_numeric(holdings_df[col], errors="coerce")
    # N-PORT uses "N/A" / zeros for missing CUSIPs
    holdings_df["cusip"] = holdings_df["cusip"].replace({"N/A": None, "000000000": None})
    # derivatives are often reported with name "N/A" and the description in title
    missing_name = holdings_df["name"].isin([None, "N/A"]) | holdings_df["name"].isna()
    holdings_df.loc[missing_name, "name"] = holdings_df.loc[missing_name, "title"]

    holdings_df["as_of_date"] = pd.Timestamp(report_date)
    holdings_df["series_id"] = series_id
    holdings_df["series_name"] = series_name
    holdings_df["net_assets"] = pd.to_numeric(net_assets, errors="coerce")
    return holdings_df


def query_nport_holdings(
    ticker: str,
    holdings_date: Union[date, str, None] = None,
    user_agent: Optional[str] = None,
    max_filings: int = 40,
) -> pd.DataFrame:
    """Retrieve a fund's holdings from its public N-PORT filing.

    Args:
    - ticker: any US registered fund ticker (ETF or mutual fund)
    - holdings_date: the reporting date (a month end) to retrieve. If not given,
    returns the latest public filing.
    - user_agent: SEC User-Agent with contact details, defaults to $SEC_USER_AGENT
    - max_filings: how many recent filings to search through

    Raises InvalidParameterError if there's no public filing for the requested date.
    """
    if isinstance(holdings_date, str) and holdings_date:
        holdings_date = pd.to_datetime(holdings_date).date()

    filings = list_nport_filings(ticker, max_filings=max_filings, user_agent=user_agent)
    if filings.empty:
        raise InvalidParameterError(f"No public N-PORT filings found for {ticker}")

    if holdings_date:
        matches = filings[filings["report_date"] == holdings_date]
        if matches.empty:
            available = ", ".join(str(d) for d in filings["report_date"].dropna())
            raise InvalidParameterError(
                f"No public N-PORT filing for {ticker} as of {holdings_date}. "
                f"Available dates: {available}"
            )
        filing = matches.iloc[0]
    else:
        filing = filings.iloc[0]

    logger.debug(
        f"Retrieving N-PORT filing {filing['accession_number']} for {ticker} "
        f"as of {filing['report_date']}"
    )
    xml = _sec_get(filing["folder_url"] + "primary_doc.xml", user_agent).content
    holdings_df = parse_nport_xml(xml)

    fund = lookup_fund(ticker, user_agent)
    ret_series = holdings_df["series_id"].iloc[0] if len(holdings_df) else None
    if ret_series and ret_series != fund["series_id"]:
        raise ValueError(
            f"N-PORT filing is for series {ret_series}, expected {fund['series_id']}"
        )

    holdings_df["accession_number"] = filing["accession_number"]
    return normalize_holdings(
        holdings_df, fund_ticker=ticker.upper(), source=SOURCE_SEC_NPORT
    )
