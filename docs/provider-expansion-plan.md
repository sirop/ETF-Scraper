# Provider Expansion Plan

Plan for extending ETF-Scraper from 4 US providers to 16 ETF issuers, including European (UCITS) funds.

Status: draft, 2026-11-10. Endpoints for the new providers are **not verified yet**. Finding them is Phase 0 for each provider.

## 1. Goal

Retrieve current holdings, and history where possible, for ETFs from these issuers:

| Group | Issuers |
|---|---|
| Usually accessible (CSV/XLSX download on the product page) | iShares (BlackRock), SPDR (State Street), Xtrackers (DWS), Invesco, VanEck, WisdomTree, Global X |
| With obstacles (JavaScript, cookie or investor-profile prompts, some geoblocking) | Vanguard, Amundi (incl. Lyxor), UBS, HSBC, JPMorgan AM, Franklin Templeton, Fidelity, L&G, BNP Paribas Easy |

Every holdings result should use one shared column format (section 4.3), whatever the source.

## 2. Current State (checked 2026-11-10)

| Provider | Region | Status | Source |
|---|---|---|---|
| SSGA (SPDR) | US | ✅ Works | XLSX download |
| Vanguard | US | ✅ Fixed, latest month end only | `investor.vanguard.com/vmf/api/{ticker}/portfolio-holding/*.json` |
| Invesco | US | ✅ Fixed, latest only | `dng-api.invesco.com` JSON API |
| iShares | US | ❌ Broken | CSV download now returns an HTML page |
| SEC N-PORT | US (any registered fund) | ✅ New | EDGAR, quarterly, about 60 days delayed |

The package currently assumes **US funds identified by ticker**. Most of the new issuers are European UCITS issuers, or have large UCITS ranges, so the architecture needs to change first (section 4).

## 3. Access Rules

These rules apply to every provider:

1. **Use only what the provider publishes publicly:** product-page downloads, the JSON the provider's own site loads, and documented APIs.
2. **Identify honestly.** Send a User-Agent that names this tool. Don't imitate a browser to get past blocking.
3. **Don't get around access controls.** No solving CAPTCHAs, no defeating bot or firewall protection, no proxies to evade geoblocking, no automated login.
4. **Investor-profile and disclaimer pages:** use a provider only if the data is reachable through a normal public request, such as a documented URL parameter or a public download link. If the provider requires an interactive confirmation (accepting that you're a professional investor, picking a country), mark it "not supported" and use a fallback (section 6).
5. **Read each provider's terms of use** before adding it. If they forbid automated access, don't add it.
6. **Keep the load low:** at most about 1 request per second per provider, retries with backoff, and caching of listings.

Given these rules, some of the providers with obstacles may only be covered through fallbacks. That's an acceptable outcome.

## 4. Architecture Changes (before adding providers)

### 4.1 Identify funds by ISIN as well as ticker

- UCITS ETFs trade under different tickers on different exchanges (Xetra, LSE, SIX, Euronext), so a ticker alone is ambiguous.
- Add `isin` as the main key in `listings.csv`. Keep `ticker` plus a new `exchange` column.
- `ETFScraper.query_holdings()` should accept a ticker or an ISIN, plus an optional `provider` and `exchange` to resolve ambiguous tickers. The existing `#FIXME` in `api.py` already suggests this.

### 4.2 Providers with regional sites

- Several issuers run separate US and EU sites with different fund ranges and formats: iShares, SPDR, Invesco, VanEck, WisdomTree, Global X, JPMorgan, Franklin, Fidelity.
- Extend `Provider` with a region, for example `Provider.IShares_US` and `Provider.IShares_EU`, or add a `region` column to the listings. Each regional site gets its own scraper class.

### 4.3 Shared holdings format

Fill the existing `FundHolding` fields and add the following:

| Column | Notes |
|---|---|
| `fund_isin`, `fund_ticker`, `as_of_date` | always required |
| `name`, `isin`, `cusip`, `sedol`, `ticker` | whichever identifiers the source provides |
| `amount`, `market_value`, `weight` | `weight` in percent |
| `currency`, `market_value_currency` | UCITS funds report in EUR, USD, GBP and others |
| `security_type`, `sector`, `location`, `exchange` | free text from the provider |
| `source` | for example `provider_api`, `provider_file`, `sec_nport` |

Add a `normalize_holdings()` helper that every scraper calls at the end. It sets column order, dtypes and missing columns, so callers always get the same layout.

### 4.4 Shared plumbing

- `http.py`:
  - one `requests.Session` per provider
  - an honest User-Agent
  - timeouts
  - retries with backoff (`tenacity` is already a dependency)
  - a rate limit per host
  - a clear error when HTML comes back instead of data, which is the failure mode we keep seeing
- `parsers.py`: reusable readers for:
  - CSV with preamble rows (iShares and others put metadata above the table)
  - XLSX with header rows
  - European number formats (`1.234,56`)
- One scraper module per provider (`scrapers/ishares.py`, ...) instead of a single 900-line `scrapers.py`.

### 4.5 Dependency fixes (do now)

- Pin `pandas_market_calendars` to a version compatible with pandas 2.3, or fix `utils._get_trd_dates`. One test currently fails because of this.
- Raise `requires-python` to 3.10 and drop the `numpy<2.0` pin, so Python 3.13+ installs work. CI tests 3.10 and 3.15.
- Open the test data files in tests with `encoding="utf-8"`, so the tests pass on Windows.

## 5. Provider Plan

### 5.1 Phase 0 for every provider: reconnaissance

Work through this checklist for each provider and record the results in a table in this file before writing a scraper:

- [ ] Product page URL pattern (US and EU site)
- [ ] Holdings source: file download (CSV/XLSX), JSON loaded by the page, or none
- [ ] Does it work with a plain request and an honest User-Agent? (yes / needs a cookie or profile / blocked)
- [ ] Terms of use: is automated access allowed?
- [ ] Fund identifier the endpoint uses (ticker, ISIN, internal ID)
- [ ] Available history: latest only, month ends, daily, date parameter
- [ ] Listing source (a full fund list for `listings.csv`)
- [ ] Save sample responses as test data

UCITS ETFs come first (decision 3). The US ranges of the new issuers move to the end, and many of them are already covered by SEC N-PORT.

### 5.2 Wave 1: UCITS issuers that are usually accessible

| # | Provider | Notes to verify |
|---|---|---|
| 1 | iShares EU (UCITS) | Largest UCITS range, highest priority. Probably the same `.ajax?fileType=csv` approach as the US site. Check whether the EU download has the same HTML-instead-of-CSV problem as the US one. |
| 2 | Xtrackers (DWS) | Holdings download on etf.dws.com. Almost entirely UCITS, so ISIN-based. |
| 3 | SPDR EU (UCITS) | Same XLSX approach as SSGA US, on the European site. |
| 4 | Invesco EU (UCITS) | Check whether `dng-api.invesco.com` also serves the EU accounts (for example `en_GB` or `de_DE` instead of `en_US`). |
| 5 | VanEck EU (UCITS) | Separate site from VanEck US. |
| 6 | WisdomTree EU (UCITS) | EU range includes ETPs (commodities, crypto) that hold no securities, so skip them. |
| 7 | Global X Europe (UCITS) | Separate site from Global X US. |

### 5.3 Wave 2: UCITS issuers with obstacles

For each one, decide after Phase 0: **supported**, **supported with limits**, or **not supported for now**. Paid data and PDF reports aren't planned as fallbacks (decisions 5 and 6), so "not supported for now" is an acceptable outcome.

| # | Provider | Expected obstacle |
|---|---|---|
| 8 | Amundi (incl. Lyxor) | Investor profile and country selection |
| 9 | UBS | Profile prompt, JavaScript-rendered pages |
| 10 | HSBC | Profile prompt |
| 11 | L&G | Profile prompt |
| 12 | BNP Paribas Easy | Profile prompt, geoblocking |
| 13 | Vanguard EU (UCITS) | Regional Vanguard sites; check whether they use an API like the US `vmf/api` |
| 14 | JPMorgan AM EU (UCITS) | JavaScript, profile prompt |
| 15 | Franklin Templeton EU (UCITS) | JavaScript |
| 16 | Fidelity EU (UCITS) | JavaScript, profile prompt |

Pages that only render with JavaScript usually still load their data as JSON from an endpoint the page calls. Phase 0 should look for that endpoint in the browser's developer tools. A headless browser is a last resort: slow, fragile, and an extra dependency.

### 5.4 Wave 3: US ranges

Lower priority. The SEC N-PORT module already provides quarterly holdings for all of them.

| # | Provider | Tasks |
|---|---|---|
| 17 | iShares US | Find out why the CSV download returns HTML: changed ID, required cookie, or blocking. Fix it only within the access rules. |
| 18 | VanEck, WisdomTree, Global X US | Holdings downloads on the US product pages. |
| 19 | JPMorgan, Franklin Templeton, Fidelity US | Only if Phase 0 finds a public source. Otherwise N-PORT. |

## 6. Fallback Data Sources

| Source | Covers | History | Limits |
|---|---|---|---|
| SEC EDGAR N-PORT (already built) | All US registered funds, including the US ranges of JPMorgan, Franklin, Fidelity, VanEck, WisdomTree and Global X | From 2019 | Quarterly, about 60 days delayed, few tickers |
| Fund annual and semi-annual reports (PDF) | UCITS funds | Twice a year | **Deferred** (decision 5). PDF table extraction is fragile and differs by issuer. |
| Commercial data (for example Morningstar or a paid ETF holdings API) | Everything | Varies | **Not planned** (decision 6). Avoid paid data. |

There is no free EU equivalent of N-PORT. UCITS holdings are only published by the issuers themselves and in fund reports. In practice, a UCITS fund whose issuer has no public holdings source stays unsupported until the PDF option is revisited.

Possible later step: make `query_holdings()` fall back automatically, first to the provider and then to N-PORT for US funds, and record which source was used in the `source` column.

## 7. Testing and Monitoring

- **Unit tests:** one parser test per provider with saved test data (at least one equity fund and one bond fund where available). Never call the network in unit tests.
- **Live checks:** extend the GitHub Actions workflows to one popular fund per provider, with a minimum row count, as the existing `check-scraper.yml` does. Run them on weekdays.
- **Sanity checks in `normalize_holdings()`:**
  - weight sum between 90 and 110 (warn otherwise; Vanguard's weight rounding is a known exception)
  - no duplicate rows
  - a single `as_of_date`
- **Updating test data:** a script that fetches fresh test data per provider, so it's easy to update when a provider changes its format.

## 8. Milestones

| Milestone | Content | Done when |
|---|---|---|
| M0 ✅ | Dependency fixes (4.5), split `scrapers.py` into one module per provider | All tests pass on Windows and Linux. Done 2026-11-10: `tests.yml` passes on Linux and Windows with Python 3.10 (pandas 2.x) and 3.15 (pandas 3.x). |
| M1 ✅ | ISIN-based listings, regional providers, `normalize_holdings()`, `http.py` (4.1–4.4) | Existing 4 providers use the new structure. Done 2026-11-10: lookup by ticker or ISIN (+ provider/exchange), `domicile` column, US ISINs derived from CUSIPs (358 filled, 9 wrong Invesco ISINs corrected), `Provider` naming convention for regional sites (`IShares_EU`), `holdings.normalize_holdings()` used by all scrapers and N-PORT, `http.ProviderSession` used by all scrapers. `parsers.py` (4.4) moved to M2, to be built against real EU provider files. |
| M2 | Wave 1, part 1: iShares EU, Xtrackers | Live checks green |
| M3 | Wave 1, part 2: SPDR EU, Invesco EU, VanEck EU, WisdomTree EU, Global X Europe | Live checks green |
| M4 | Phase 0 for all of Wave 2, with a supported / limited / not supported decision for each | Recon table filled in |
| M5 | Wave 2 scrapers for the supported UCITS providers | Live checks green |
| M6 | Wave 3: US ranges (iShares US fix, other US sites); automatic N-PORT fallback for US funds | Live checks green |
| M7 | Deferred: extraction from PDF reports for UCITS providers without a public source | Revisit after M5 |

## 9. Decisions

### Decided (2026-11-10)

1. **Branch:** work continues on `fix/vanguard-invesco-sources`.
3. **Focus:** UCITS ETFs first. US ranges come last (Wave 3).
5. **PDF reports:** deferred. Revisit after M5.
6. **Paid data:** avoided. Only free public sources.
2. **Fork:** work is pushed to https://github.com/sirop/ETF-Scraper (git remote `fork`). `origin` stays the upstream repo.

### Still open

4. **History:** is the latest holdings enough, or do we need a daily or monthly archive? Most UCITS issuers probably publish only current holdings, so an archive means saving results on a schedule. Pending: choice of cloud storage location. Until then, scrapers should return dataframes and keep storage separate (the existing `storage.py`), so any backend can be plugged in later.
