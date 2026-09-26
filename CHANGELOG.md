# Changelog

## 0.5.0 — 2026-09-26

Fixes from a full real-user test pass, and a consolidation of the tool
surface from 38 tools to 30 with no loss of capability. **Breaking:** eight
tools were removed or merged — see the migration table below.

### Migration

| Removed tool | Use instead |
|---|---|
| `compare_stocks_ui` | `compare_companies` — now renders the dashboard in MCP Apps hosts and returns the same JSON everywhere; takes 2–6 symbols as a list or comma-separated string |
| `explain_for_beginners` | `get_company_overview` or `get_full_analysis` — ask Claude to explain for a beginner |
| `search_shareholder` | `get_bulk_deals(name=...)` — `symbol` and `name` are both optional filters |
| `get_credit_ratings` | `get_company_announcements(symbol, category="credit_rating")` (defaults to a 730-day window) |
| `get_promoter_pledge_history` | `get_shareholding_pattern` → `data.pledge` |
| `analyze_annual_report` | `ask_company_research(symbol, question, year=2024)` |
| `analyze_earnings_call` | `ask_company_research(symbol, question, quarter="Q3FY25")` |
| `list_investment_themes` | every theme's criteria are in `screen_by_theme`'s description |

### Fixed
- **Pledges were never detected.** Screener's shareholding table has no
  pledge row even when 89% of promoter shares are pledged, so every company
  read as "no pledge". Pledges now come from Screener's analysis ("Promoters
  have pledged 89.4% of their holding"), and a company with no promoter
  group (ITC) gets `not_applicable` instead of an implied "clean".
- **Sector themes didn't match their names.** `defense`, `ev_theme`,
  `chemicals`, `railways` and `renewable_energy` were generic growth screens
  (`defense` returned SPARC and Ksolves). They now start from the companies
  actually in the sector — Nifty India Defence / Nifty EV & New Age
  Automotive / Nifty Chemicals constituents, Screener industry pages
  (Aerospace & Defense, Shipbuilding, Specialty Chemicals, Railway Wagons),
  or a curated list where neither exists — and apply filters to them.
  `data.universe` says which.
- `rising_profit_falling_price` now requires the price to have fallen
  (`Return over 1year < 0`); `improving_roce` requires ROCE above last
  year's and its 5-year average; `dividend_aristocrats` requires a dividend
  in each of the last two years and a 3-year payout above 20%.
- **Ratios history** promised P/E, P/B and ROE but only had working-capital
  days and ROCE. It now adds ROE % and debt-to-equity (computed from the
  statements) and year-end P/E and P/B (Screener's chart API), with
  `row_sources` naming the source of every row.
- **`years` counted TTM as a year** (`years=3` gave two years plus TTM). TTM
  is now returned separately as `data.ttm`.
- Row labels no longer carry Screener's `+` expand suffix ("Sales +"), and
  the quarterly table's empty "Raw PDF" row is gone.
- **`get_full_analysis` disagreed with the standalone tools**: its peer
  section said "loads via AJAX" and was empty, and it showed 6 quarters of
  shareholding instead of 8 and 9 years of P&L. It is now built from the
  same helpers (10 fiscal years + TTM, 8 quarters, real peer table).
- **`analyze_red_flags` computed nothing** — it was `get_full_analysis` plus
  a prompt. It now runs rule-based checks over the full history (promoter
  holding, pledge, leverage, borrowings vs sales, ROCE trend and level,
  cash conversion, negative CFO, growth quality, debtor/inventory days,
  other-income dependence, dilution), each with severity, evidence and the
  threshold used; checks that don't apply (leverage for banks, promoter
  checks for ITC) are listed as skipped, not passed.
- **ESG scores were listed as credit ratings** (NSE files both under
  "Credit Rating"). They're now a separate `esg_rating` category.
- **Insider trading missed disclosures**: only NSE's newer PIT feed was
  read. Both PIT feeds are now merged and de-duplicated.
- **News for companies named after a common word** was mostly noise — the
  RELIANCE query matched "self-reliance" and "AI reliance", and "ITC"
  matched GST input-tax-credit stories. The NSE code is no longer searched
  when it's just a word of the name, acronym names are anchored ("ITC Ltd",
  "ITC shares"), and headlines that don't name the company are dropped
  (`dropped_off_topic` reports how many).
- **Document analysis**: two-column pages were read straight across and
  interleaved; they're now split at the gutter. Mirrored/rotated decorative
  text ("GNIYFITROF") is dropped. BRSR boilerplate is ranked down unless
  the question is about ESG, hits are spread across the document, and each
  excerpt is capped at ~900 characters around the question's terms.
  Indexes built by 0.4 are rebuilt automatically on next use.
- `get_document_list` listed years twice (a .zip and a .pdf); it now keeps
  one per year, preferring the PDF.
- `get_commodity_prices` suggested unrelated screens (a chemicals screen for
  cotton). It now returns the exposed companies' symbols
  (`data.watch_symbols`), adds wheat (CBOT) and exposure-only entries for
  leaf tobacco and wood pulp, and no longer claims MCX contracts that don't
  exist.
- Screens flag dividend yields above 25% (special dividends or stale
  prices) as implausible.

### Added
- `page` on `screen_stocks` and `screen_by_theme` to go past the first page.
- `get_bulk_deals` searches up to 90 days (was 30).
- `get_company_overview` includes Screener's pros/cons (`screener_analysis`).
- `get_shareholding_pattern` returns per-category trends (window and
  last-quarter change) and promoter-group detection.
- `get_insider_trading(days=...)`.

### Not reproduced
- The tester's NSE failures (announcements, bulk deals, shareholder search,
  credit ratings all empty), the blank company description / NSE-BSE codes,
  and blank `debt_to_equity` were already fixed on `main` before this pass
  (the test ran against an older build). The live canary now covers them.
- The 4-minute hang on the first `add_portfolio_stock` call: that path does
  no network I/O in current code; the stall matches the old 240-second tool
  timeout on the older build.

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
