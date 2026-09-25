# Changelog

## 0.3.0 — unreleased

### Breaking
- **Every tool returns a JSON envelope** instead of a markdown string:
  `{status, partial, warnings, data, missing_fields?, reason?, meta?, error?}`.
  `status` is `ok`, `partial` or `error`. Narrative tools (full analysis, red
  flags, beginner explainer, document Q&A, notebook) keep their text in
  `data.report`. The other tools now return structured data:
  - quarterly results and shareholding: `quarters` + `rows`
  - pledge history, peers, company comparison, document list, portfolio
  - announcements, bulk deals and insider trades
- Company comparison output is `data.companies[]` (was a text table).
- The `screener://stock-comparison` resource is replaced by the MCP App
  resource `ui://screener/stock-comparison.html`.

### Fixed
- **Silent empty data.** Companies without subsidiaries (MSUMI, GRSE) have
  a blank `/consolidated/` page on Screener. The server now falls back to
  standalone figures and says so. Remaining blanks are listed in
  `missing_fields` with `status: partial` and `null` values.
- **Symbol resolution.** Near-miss symbols and company names resolve
  through Screener search (`MOTHERSONWIR` → `MSUMI`). Ambiguous input
  returns the top 3 candidates. This applies to the NSE-backed tools too.
- **Implausible ratios** are marked `data_quality_flag: true`. That covers
  out-of-range days metrics and a Cash Conversion Cycle that doesn't match
  its components. These checks are skipped for banks, NBFCs and insurers.
- **NSE failures no longer look like "no data".** A block or rate limit
  returns `upstream_unavailable`, and failed bulk-deal days return `partial`.
- **Commodity prices.** The MCX scrape almost never returned a price. Prices
  now come from international benchmark futures, with period moves and an
  approximate INR conversion.
- **Portfolio P&L.** A holding with no live price no longer counts as a
  total loss in the totals. Prices are also fetched in parallel.
- **Debt-to-equity.** Comparisons and the dashboard compute it from the
  balance sheet (Screener's top ratios don't include it). It's `null` for
  financial companies.
- The company "about" text parses again after a Screener layout change.
- **Rate limiting.** Requests are paced (`SCREENER_MIN_INTERVAL`) and capped
  (`SCREENER_MAX_CONCURRENCY`). Any 429 now pauses every in-flight request
  for a shared cooldown, where before each request backed off on its own
  while the rest kept hitting Screener. Timeouts are retried too. Before
  this, a cold 50-stock screen from a fresh IP got only 16 price histories.

### Added
- **Technical clauses in `screen_stocks`:** 52-week low/high distance, RSI,
  price vs 20/50/200 DMA, and volume vs its 20-day average. Technical-only
  screens scan an NSE index and need no login.
- **`get_52_week_low_candidates`**: quality stocks near their 52-week low,
  in one call.
- **`compare_to_sector`**: a stock's move vs its sector index and the
  Nifty 50, with a market-vs-company-specific verdict.
- **Price-history cache** (`~/.screener-mcp/price_cache`). During market
  hours entries expire after 15 minutes; outside market hours they last
  until the next session. Disable it with `SCREENER_PRICE_CACHE=0`.
- **Document index freshness.** Results report `last_indexed_at`, the
  content hash, ETag and `source_changed`. `force_reindex` rebuilds an index.
- **Interactive dashboard** for `compare_stocks_ui`, delivered as an MCP App
  with the ext-apps runtime vendored into the package.
- **Daily live canary** (`scripts/canary.py`, `.github/workflows/canary.yml`).
  It opens a GitHub issue when Screener's pages stop parsing.
