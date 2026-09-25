# Changelog

## 0.4.0 — 2026-09-26

### Added
- **`get_analyst_targets`** returns two views. The first is Yahoo Finance's
  consensus: mean, median, high and low target, analyst count, implied
  upside and the buy/hold/sell split. The second is broker target prices
  pulled from the last 60 days of headlines, with article links. Figures
  that can't be targets, such as capex amounts in crores, are filtered out.
  If one source is down the result is `partial`.
- **`get_recent_news`**: recent headlines from Google News (Indian edition),
  de-duplicated and newest first, with publisher, time and link.
- **`get_relative_valuation`**: P/E and ROCE compared with the median of
  every listed company in the stock's Screener industry, for up to 20
  stocks. Stocks in the same industry share one cached fetch. Each stock
  gets an assessment, including a possible "value trap" warning when a
  low P/E comes with low ROCE. `screen_stocks(peer_relative=True)` adds
  the same data to screen results.
- **`get_moat_signals`** covers two things, with the thresholds stated in
  the output:
  - industry position: revenue share, rank, HHI concentration and CR4
    across the whole industry;
  - durability: ROCE consistency, operating-margin stability, sales CAGR
    and promoter-holding stability.
- **`get_forward_outlook`** has four independent parts:
  - analyst EPS and revenue estimates for this year and next, with forward P/E;
  - order wins from NSE filings, summing any rupee values stated in headlines;
  - capex and expansion filings;
  - earnings-call passages on guidance, order book and capex (needs the
    `[ai]` extra).
- **`screen_stocks` accepts `OR` and parentheses** alongside technical
  clauses. Purely fundamental logic passes through to Screener unchanged.
  When a technical clause sits under an `OR`, each alternative runs as its
  own screen and the results are merged, with `matched_groups` on each row.
- **Screen hygiene.** A default `min_market_cap` of ₹100 Cr is added unless
  the query sets its own. Rows with implausible numbers (P/E below 1,
  one-off profit spikes on a small base, profit above sales, negligible
  sales, sub-₹1 price) are excluded and listed; `exclude_flagged=False`
  keeps them, flagged.
- **Price freshness in `get_company_overview`.** `data.price_freshness`
  gives `price_as_of`, whether the price is an intraday print or the last
  close, the previous close and the day's change. Stale prices are flagged.

> 0.3.0 was tagged but never reached PyPI: the publish token was rejected.
> 0.4.0 includes everything from 0.3.0.

## 0.3.0 — 2026-09-25

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
