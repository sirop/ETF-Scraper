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

### 5.1a Reconnaissance results

| Provider | Holdings source | Plain request + honest User-Agent | robots.txt / terms | Fund identifier | History | Fund list source | Decision |
|---|---|---|---|---|---|---|---|
| iShares EU (checked 2026-11-10) | `<product page>/latest-holdings.csv`, published on each page as a schema.org `DataDownload` ("Latest fund holdings CSV data") | ✅ works | ✅ not disallowed | product page URL (ID + slug). Use the exact URL: the cache is per URL, other slugs can return an older file | latest only | UK product sitemap (915 UCITS ETF pages incl. share classes) + start of each page (schema.org data for ticker/name/asset class, page header for ISIN) | **Supported** (`ISharesEUListings`) |
| iShares US (checked 2026-11-10) | the old `.ajax?fileType=csv` URL returns HTML, but `<product page>/latest-holdings.csv` works (newer CSV layout) | ✅ works | ✅ not disallowed | product page URL | latest only via this link | US screener JSON still works | Fixed 2026-11-11 (M6): holdings from `latest-holdings.csv` (equity layout with `Type`, `Market Weight`, `Notional Weight`; bond, commodity and mutual fund layouts); latest only; fund list refreshed (526 funds) |
| SPDR EU / State Street (checked 2026-11-10) | daily holdings Excel linked from each fund page: `/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-<bloomberg ticker>.xlsx` (eg `spy5-gy`); includes each holding's ISIN and SEDOL | ✅ works | ✅ not disallowed | Bloomberg style ticker (eg `SPY5 GY`), file also states the fund ISIN | latest only | public fund finder JSON for Germany (180 UCITS ETFs, IE and LU domiciled) + each fund page for the ISIN | **Supported** (`SSGAEUListings`, provider `SSGA_EU`) |
| Invesco EU (checked 2026-11-10) | the JSON API the fund pages use: `dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/<ISIN>/holdings/fund?idType=isin` (no `productType`, unlike the US); name, ISIN, weight, currency only (no shares or market values). Synthetic ETFs return their substitute basket; `holdings/index` gives index constituents | ✅ works | ✅ invesco.com robots.txt doesn't cover these paths; the API host has none | ISIN | latest only | `/product/search` for the Irish site (306 open share classes incl. a few XS-ISIN commodity ETCs without holdings) | **Supported** (`InvescoEUListings`, provider `Invesco_EU`) |
| VanEck EU (checked 2026-11-10) | the "Download All Holdings" Excel each fund page offers: `/ie/en/investments/<fund slug>/downloads/holdings/` (eg `semiconductor-etf`); English file has name, ticker, ISIN, shares, market value, % of net assets and the as-of date. Use the `/ie/en/` path: `/ucits/` redirects by location (to German formats from Germany) | ✅ holdings download works | ✅ download paths not disallowed; **`Crawl-delay: 25`** (one request per 25 s) | fund page slug | latest only | ❓ the fund explorer loads its list with a POST to `/Main/FundListingUcits/GetFundData/`, which rejects a plain request (HTTP 405 error page, not a bot challenge). A retry with the page's AJAX headers (`X-Requested-With`, `Referer`; run by the user) also got the 405, so the endpoint only answers real browser sessions; not pursued further (access rule 3) | **Parked** (2026-11-10, like Xtrackers). Revisit options: a user maintained fund list on top of the working holdings download, or asking VanEck for a fund list/data feed |
| Global X Europe (checked 2026-11-10) | CSV downloads on each fund page: `/api/funds/<fund>/topholdingscsv` (all holdings of physical funds, with ISIN/SEDOL/shares/market value) or, when that only contains the disclaimer, `/api/funds/<fund>/basketconstituentscsv` (substitute basket of synthetic funds such as QYLD; 9 character ids in the SEDOL column are CUSIPs) | ✅ works | ✅ `robots.txt` allows all | fund page id (eg `botz`) | latest only | sitemap (53 fund pages) + each page for primary and distributing ISIN | **Supported** (`GlobalXEUListings`, provider `GlobalX_EU`) |
| WisdomTree EU (checked 2026-11-10) | — | ❌ every page (eg `/en-ie/etfs`) returns HTTP 403 "Just a moment..." (Cloudflare bot challenge) | ❌ `robots.txt` disallows `/api` | — | — | — | **Not supported** (access rule 3: no getting past bot challenges). Revisit only with permission or a data feed from WisdomTree |
| Xtrackers (checked 2026-11-10) | product pages are a JavaScript shell; data comes from `etf.deutsche-am.com/api/` | not tried | ❌ `robots.txt` disallows `/api/`, `/etfdata/` and `/export/`; site has an investor entry gate | ISIN in page URL | — | — | **Not supported for now** (access rules 3 and 5). Public example code (eg riccardotornesello/portfolio-scraper) uses exactly the disallowed `/api/fundfinder/...` and `/etfdata/export/.../constituent/{isin}/` paths plus a hard-coded investor-profile cookie. Deferred (2026-11-10): revisit later; options are asking DWS for permission or a data feed, or an opt-in scraper after checking DWS's terms of use. |
| Amundi incl. Lyxor (checked 2026-11-10, M4) | product pages (eg `/de/privatanleger/products/equity/<slug>/<isin>`) are an Angular widget that loads everything, holdings included, with `POST /mapi/ProductAPI/getProductsData` (`productIds: [ISIN]`, `composition.compositionFields`: date, type, bbg, isin, name, weight, quantity, currency, sector, country). The request carries the page context `{countryCode: "DEU", languageCode: "de", userProfileName: "RETAIL"}`, which the public retail page embeds itself | ✅ robots.txt, sitemap and product pages load (no bot challenge) | ✅ `robots.txt` only disallows search query strings (`?isin=`, `?searchedWord=`...); `/mapi/` not disallowed | ISIN (in page URL and API) | latest only (one as-of date per response) | sitemap `/sitemap/sitemap.xml` (622 product URLs on the German site, ISIN in each URL) + the same API in batches of 50 for names and tickers | **Supported** (`AmundiListings`, provider `Amundi`). The session's permission check blocked my test request; the user ran it: HTTP 200 JSON, 66 holdings for LU1681038839 (swap based, so the substitute basket), weights as fractions summing to 0.985. Fund list built with `scripts/build_listings.py Amundi` (622 share classes) |
| UBS (checked 2026-11-11, M4) | fund data only inside the "fundgate" app on `/<country>/<lang>/assetmanagement/funds/etf.html`, which shows "funds details and prices for those funds available in your jurisdiction and for your investor type" after the visitor picks a country and investor type and accepts a disclaimer | ✅ pages load (no bot challenge) | ✅ only paths unrelated to ETFs disallowed | — | — | ❌ the country sitemaps (`de/de`, `ch/de`, `ie/en`, `lu/en`, `global/en`) list ETF topic pages but no fund pages | **Not supported for now** (access rule 5: the data sits behind an investor type and jurisdiction gate; unlike Amundi there is no public page that carries a fixed retail context). Revisit like Xtrackers: ask UBS for a data feed, or an opt-in after checking the terms |
| HSBC (checked 2026-11-11, M4) | ✅ "holdings" document linked on each fund page (`/de/individual-investors/funds/<isin>`): `GET /api/v1/download/document/<isin>/de/de/holdings` returns an `.xls` (old Excel format) with fund name, date and fund size, then ISIN, CUSIP, name, shares, market value, country, currency, weight per holding (MSCI World: 1,300+ rows incl. tax receivables). Needs `xlrd` (or `python-calamine`) to read | ✅ plain GETs work (no bot challenge) | ✅ allowed; **`Crawl-delay: 2`** | ISIN (fund page and download) | latest only | ❌ no fund pages in the sitemap; the fund list comes from `/api/v1/nav/funds`, which needs a bearer token from `POST /api/v1/token/issue` with a deliberate quota (HTTP 429 shows a "quota" popup) | **Supported** (`HSBCListings`, provider `HSBC`), decided with the user 2026-11-11: not automating the quota protected token flow; instead `data/hsbc_etfs.csv` lists 86 share class ISINs and names from HSBC ETFs plc's 2026 AGM pack (plus 2 seen on HSBC's ETF pages), and `retrieve_listings` keeps those with a German fund page that links holdings (67). Not kept: mostly GBP/CHF hedged and Islamic share classes (presumably not registered in Germany) and FTSE 250. No tickers (lookup by ISIN). Refresh the seed list from the next AGM pack |
| L&G (checked 2026-11-11, M4) | fund pages (eg `fundcentres.landg.com/en/de/private-investors/fund-centre/ETF/Artificial-Intelligence/IE00BK5BCD43/`) show the **top 10 holdings** (name, weight) in their HTML; the "Download full fund holdings" CSV is at `/srp/documents-id/<id>/Fundholdings.csv` | ✅ pages load (no bot challenge) | ❌ for full holdings: `robots.txt` disallows `/srp/documents-id/`, `/srp/documents/`, `/srp/api/`; fund pages allowed | ISIN in page URL | latest only | ✅ per-country sitemaps list the ETF pages with ISINs (~120–140 per section) | **Limited**: only the top 10 holdings without the disallowed CSV. Not implemented for now; revisit by asking L&G for permission to use the CSV, or implement top 10 if that is useful |
| BNP Paribas Easy (checked 2026-11-11, M4) | — | ✅ pages load | ❌ `robots.txt` disallows every URL with a query string (`/*?`) and `/*.php$`; the fund explorer loads its data through `/wp-admin/admin-ajax.php` | — | — | ❌ the `funds-sitemap.xml` files are empty; the German sitemap has only a few Easy landing pages | **Not supported** (access rules 1 and 5: data only through disallowed paths, and pages ask "Welcher Anlegertyp sind Sie?") |
| Vanguard EU (checked 2026-11-10, M4) | — | — | ❌ `de.vanguard` and `vanguard.co.uk`: `User-agent: *` shares a group with the AI crawlers that ends in `Disallow: /`, so all automated clients are excluded from the whole site | — | — | — | **Not supported** (access rule 1). European Vanguard ETFs have no N-PORT filings either |
| JPMorgan AM EU (checked 2026-11-10, M4) | — | one product explorer page loaded (fetched in the same pass, before robots.txt was evaluated) | ❌ `robots.txt` disallows `/asset-management/` and `/*/asset-management/` for all clients, which covers every fund page | — | — | — | **Not supported** (access rule 1) |
| Franklin Templeton EU (checked 2026-11-10, M4) | product pages are a single page app; holdings come from the GraphQL query `Holdings` (`Portfolio(fundid, countrycode, languagecode)` → `fullholdings` / `dailyholdings` with ticker, ISIN, CUSIP, name, quantity, weight, market value, currency, asset class) via `POST /api/pds/price-and-performance` | ✅ sitemap and pages load (no bot challenge) | ✅ `franklintempleton.de` robots.txt lists only a sitemap (nothing disallowed) | fund id + share class code + ISIN, all in the product URL (`/preise-etfs/produkt/<fund id>/<share class>/<slug>/<ISIN>`) | latest only (`dailyholdings`; `fullholdings` was empty for the tested fund) | the `IntlIndentifiers` query (`PPSS(productType: "etf")`) returns all 62 share classes (49 funds) with fund id, ticker, ISIN, Bloomberg ticker, SEDOL in one request; sitemap `de-de_product.xml` for the page URLs | **Supported** (`FranklinEUListings`, provider `Franklin_EU`). The user ran the test (my POST would be blocked by the permission check): HTTP 200 JSON with `languagecode: "de_DE"`, 1636 daily holdings for fund 40163, weights summing to 100.3; values use German number formats and "—" for missing; the id column holds SEDOLs (7 characters) or CUSIPs (9) |
| Fidelity EU (checked 2026-11-10, M4) | — | ❌ ETF pages redirect to `partner.fidelity.de`, which returns an Akamai "Access Denied" page; `fidelityinternational.com` (where the fund factsheets live) returns "Access Denied" even for `robots.txt` | ✅ `fidelity.de` disallows `/tools*` only | — | — | `fidelity.de` sitemap has topic pages only, no fund pages; its fund finder is a brokerage tool covering many providers | **Not supported** (access rule 3: bot block) |

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
| M2 🟡 | Wave 1, part 1: iShares EU, Xtrackers | Live checks green. 2026-11-10: iShares EU done (915 UCITS ETFs in `listings.csv`, `parsers.py` added); Xtrackers not supported for now (robots.txt, see 5.1a). Open: live check workflow for iShares EU. |
| M3 ✅ | Wave 1, part 2: SPDR EU, Invesco EU, VanEck EU, WisdomTree EU, Global X Europe | Done 2026-11-10: SPDR EU (`SSGA_EU`, 180 UCITS ETFs), Invesco EU (`Invesco_EU`, 306 share classes), Global X Europe (`GlobalX_EU`, 61 share classes incl. 5 crypto ETPs) supported; VanEck EU parked (fund list endpoint only answers browser sessions); WisdomTree EU not supported (Cloudflare bot challenge). Open: live check workflows for the new providers. |
| M4 ✅ | Phase 0 for all of Wave 2, with a supported / limited / not supported decision for each | Done 2026-11-11. Supported: Amundi (622 share classes), Franklin Templeton (61), HSBC (67, fund list from a seed list of ISINs). Limited: L&G (top 10 holdings only; full holdings CSV is robots-disallowed; not implemented). Not supported: Vanguard EU and JPMorgan AM EU (robots.txt), Fidelity EU (bot block), UBS (investor gate), BNP Paribas Easy (disallowed paths, investor gate). |
| M5 | Wave 2 scrapers for the supported UCITS providers | Live checks green |
| M6 🟡 | Wave 3: US ranges (iShares US fix, other US sites); automatic N-PORT fallback for US funds | Live checks green. 2026-11-11: iShares US fixed (latest holdings only, from `latest-holdings.csv`; all asset classes incl. multi asset, commodities and mutual funds; closed funds raise a clear error); US fund list refreshed (491 -> 526 funds). N-PORT fallback done: `ETFScraper.query_holdings` retrieves past dates of US funds from SEC N-PORT when the provider has none (needs `SEC_USER_AGENT`; `source` = `sec_nport`; filing dates cached per accession number). Open: other US sites. |
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
