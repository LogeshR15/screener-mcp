"""
Screener.in MCP Server — Indian Stock Research Assistant

Tools exposed to Claude (38 total):
  search_company              — find a company by name or symbol
  get_company_overview        — key ratios, about, price data
  get_financials              — P&L / Balance Sheet / Cash Flow / Ratios history
  get_quarterly_results       — last 8 quarters of results
  get_shareholding_pattern    — promoter / FII / DII / public holding trend
  get_peer_comparison         — peer comparison table
  compare_companies           — side-by-side comparison of 2-5 companies
  screen_stocks               — Screener.in query + technical clauses, AND/OR/parentheses, junk filtering
  get_52_week_low_candidates  — quality stocks near their 52-week low, in one call
  compare_to_sector           — stock's move vs its sector index and Nifty 50
  get_recent_news             — recent news headlines (Google News)
  get_analyst_targets         — consensus target price + broker targets in recent headlines
  get_relative_valuation      — P/E and ROCE vs industry median, up to 20 stocks at once
  get_moat_signals            — revenue share / rank / HHI + ROCE, margin, promoter durability
  get_forward_outlook         — analyst estimates, order wins, capex filings, management guidance
  screen_by_theme             — pre-built thematic screens
  list_investment_themes      — list available theme screens
  get_full_analysis           — ALL data for deep-dive reasoning
  analyze_red_flags           — structured red flag checklist data
  explain_for_beginners       — data + prompt for beginner-friendly explanation
  compare_stocks_ui           — interactive comparison dashboard (Claude Desktop)
  get_document_list           — list annual reports and earnings call transcripts
  analyze_annual_report       — ask questions over annual report PDFs (RAG)
  analyze_earnings_call       — ask questions over earnings call transcripts (RAG)
  ask_company_research        — ask questions across ALL of a company's cached documents at once
  search_market_commentary    — semantic search for a question across multiple companies' indexed documents
  get_company_announcements   — fetch recent NSE corporate announcements
  search_shareholder          — find bulk deal activity by investor name
  get_bulk_deals              — all bulk deals for one company (no investor name needed)
  get_insider_trading         — SEBI PIT promoter/KMP/designated-person trade disclosures
  get_promoter_pledge_history — dedicated promoter pledge % trend with severity flag
  get_credit_ratings          — CRISIL/ICRA/CARE rating actions, a debt-quality check
  get_commodity_prices        — commodity price context and company impact analysis
  notebook_ai                 — save and summarize investment research notes
  add_portfolio_stock         — add/merge a holding into your local portfolio
  update_portfolio_stock      — correct quantity/avg price on an existing holding
  remove_portfolio_stock      — remove a holding entirely
  get_portfolio                — view holdings with live P&L and weights

Resources:
  screener://analyst-guide    — how to use this assistant
  screener://query-syntax     — Screener query language reference
  ui://screener/stock-comparison.html — interactive dashboard (MCP App) for compare_stocks_ui

Setup:
  Set environment variables:
    SCREENER_USERNAME=your@email.com
    SCREENER_PASSWORD=yourpassword
  Without credentials, public data only (some metrics may be hidden).

  For document analysis (analyze_annual_report, analyze_earnings_call):
    pip install pdfplumber sentence-transformers chromadb

Run:
  python -m screener_mcp.server
  or via Claude Code MCP config (see README).
"""

import httpx
import os
import re
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .core.envelope import ToolError, ToolResult, error_envelope, to_envelope
from .core.nse_client import NSEError
from .tools.company_tools import (
    search_company as _search_company,
    get_company_overview as _get_overview,
    get_financials as _get_financials,
    get_quarterly_results as _get_quarterly,
    get_shareholding as _get_shareholding,
    get_peers as _get_peers,
    compare_companies as _compare,
    get_promoter_pledge_history as _get_pledge_history,
)
from .tools.screening_tools import (
    screen_stocks as _screen,
    screen_by_theme as _theme,
    list_themes as _list_themes,
)
from .tools.technical_tools import (
    get_52_week_low_candidates as _get_52w_low,
    compare_to_sector as _compare_to_sector,
)
from .tools.research_tools import (
    get_relative_valuation as _get_relative_valuation,
    get_moat_signals as _get_moat_signals,
    get_forward_outlook as _get_forward_outlook,
)
from .tools.market_tools import (
    get_recent_news as _get_recent_news,
    get_analyst_targets as _get_analyst_targets,
)
from .tools.analysis_tools import (
    get_full_analysis as _full_analysis,
    get_red_flags as _red_flags,
    beginner_explainer as _beginner,
)
from .tools.documents import (
    get_document_list as _get_document_list,
    analyze_annual_report as _analyze_annual_report,
    analyze_earnings_call as _analyze_earnings_call,
    ask_company_research as _ask_company_research,
    search_market_commentary as _search_market_commentary,
)
from .tools.announcements import (
    get_company_announcements as _get_announcements,
    get_credit_ratings as _get_credit_ratings,
)
from .tools.shareholders import (
    search_shareholder as _search_shareholder,
    get_bulk_deals as _get_bulk_deals,
)
from .tools.insider_trading import get_insider_trading as _get_insider_trading
from .tools.commodities import get_commodity_prices as _get_commodity_prices
from .tools.notebook import notebook_ai as _notebook_ai
from .tools.portfolio import (
    add_portfolio_stock as _add_portfolio_stock,
    update_portfolio_stock as _update_portfolio_stock,
    remove_portfolio_stock as _remove_portfolio_stock,
    get_portfolio as _get_portfolio,
)

def _safe(result):
    """Wrap a tool implementation so it always returns the standard envelope
    (see core/envelope.py) and network/auth errors become structured errors."""
    import functools
    async def wrapper(*args, **kwargs):
        try:
            return to_envelope(await result(*args, **kwargs))
        except ToolError as e:
            return error_envelope(e.message, e.error_type, **e.details)
        except NSEError as e:
            return error_envelope(
                f"{e}. This is an NSE-side failure, not an empty result — retry shortly.",
                "upstream_unavailable",
                source="nse",
            )
        except PermissionError as e:
            return error_envelope(
                f"Login required. {e} Set SCREENER_USERNAME and SCREENER_PASSWORD env vars, then restart the server.",
                "login_required",
            )
        except httpx.TimeoutException:
            return error_envelope("Request timed out — Screener.in is taking too long to respond. Try again in a moment.", "timeout")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 404:
                return error_envelope("Not found. Check the symbol, or use search_company() to find the NSE/BSE code.", "not_found")
            if code == 429:
                return error_envelope("Screener.in is rate-limiting requests (HTTP 429). Wait a minute and retry, or narrow the request.", "rate_limited")
            return error_envelope(f"Screener.in returned HTTP {code}. The site may be down — try again shortly.", "upstream_error", http_status=code)
        except httpx.TransportError as e:
            return error_envelope(f"Cannot reach the data source ({type(e).__name__}). Check your connection and try again.", "network_error")
        except ImportError as e:
            return error_envelope(str(e), "missing_dependency")
        except Exception as e:
            return error_envelope(
                f"Unexpected error: {type(e).__name__}: {e}. If this persists, please open an issue at "
                "https://github.com/LogeshR15/screener-mcp/issues",
                "unexpected_error",
            )
    functools.update_wrapper(wrapper, result)
    return wrapper


_UI_DIR = Path(__file__).parent / "ui"
STOCK_COMPARISON_WIDGET_URI = "ui://screener/stock-comparison.html"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"


mcp = FastMCP(
    "Screener Stock Research",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    # Prefer generic PORT/MCP_PORT env vars used by most hosts (Render, Railway, Fly.io, etc.).
    port=int(
        os.getenv("PORT")
        or os.getenv("MCP_PORT")
        or "8000"
    ),
    instructions="""
You are an expert Indian equity analyst with deep knowledge of Indian stock markets,
Screener.in data, and long-term investing principles.

When users ask about stocks, companies, or investment themes:
1. Use the appropriate tools to fetch real Screener.in data
2. Reason over the data like a seasoned analyst
3. Present findings clearly — use tables, bullet points, and plain language
4. Always contextualize numbers (e.g., "ROCE of 25% is excellent — above industry average")
5. Flag important caveats (data is point-in-time, past performance ≠ future results)
6. For beginners, translate jargon into everyday language
7. For advanced users, go deeper into trends and red flags

You have access to all financial statements, ratios, shareholding data, and screening capabilities.
The data comes from Screener.in and covers BSE/NSE listed Indian companies.

Always be honest about limitations: Screener data may lag by a quarter, sector classifications
are broad, and you cannot predict stock prices.
""",
)


# ─── Search & Discovery ────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Search Company", "readOnlyHint": True, "openWorldHint": True})
async def search_company(query: str) -> dict:
    """
    Search for an Indian stock/company by name or NSE/BSE symbol.

    Examples:
      search_company("Tata Consultancy")
      search_company("INFY")
      search_company("Jyothy Labs")
      search_company("ITC")
    """
    return await _safe(_search_company)(query)


@mcp.tool(annotations={"title": "Screen Stocks", "readOnlyHint": True, "openWorldHint": True})
async def screen_stocks(
    query: str,
    limit: int = 25,
    sort_by: str = "",
    order: str = "desc",
    universe: str = "",
    max_candidates: int = 150,
    min_market_cap: float = 100,
    exclude_flagged: bool = True,
    peer_relative: bool = False,
) -> dict:
    """
    Run a stock screen: Screener.in fundamental fields and/or technical
    (price-action) clauses, combined with AND, OR and parentheses.

    Fundamental fields (Screener.in syntax, exact spelling matters):
      Market Capitalization, Current Price, Price to Earning, Price to book value,
      Return on capital employed, Return on equity, Debt to equity,
      Sales growth 5Years, Sales growth 3Years, Sales growth last year,
      Profit growth 5Years, Profit growth 3Years, Profit growth last year,
      Dividend yield, Pledged percentage, Net cash flow last year,
      Average return on equity 5Years, Average return on capital employed 5Years,
      Current ratio, EV / EBITDA, PEG Ratio

    Technical clauses (evaluated from daily price history):
      52 week low distance < 10        — % above the 52-week low
      52 week high distance > 30       — % below the 52-week high
      RSI < 30                         — 14-day RSI
      Price above 200 DMA              — also: below, 20/50/200 DMA, "50 DMA above 200 DMA"
      Price vs 50 DMA < -5             — % above(+)/below(−) a DMA
      Volume vs 20 day average > 2     — today's volume ÷ 20-day average (spike detection)

    Operators: > < >= <= =, combined with AND / OR and parentheses, e.g.
      "(Return on capital employed > 20 OR Return on equity > 25) AND Debt to equity < 0.5"
      "Return on capital employed > 15 AND (RSI < 30 OR 52 week low distance < 5)"
    Purely fundamental OR logic runs natively on Screener; when technical
    clauses sit under an OR, each alternative runs as its own screen (up to 6)
    and results are merged — each result lists matched_groups.

    Result hygiene (on by default):
      min_market_cap: ₹ Cr floor added to the Screener query unless the query
                      already has a Market Capitalization clause (0 = off)
      exclude_flagged: drop rows with implausible numbers (P/E < 1, >500%
                      profit spike on a small base, profit > sales, negligible
                      sales, sub-₹1 price) — they're listed in
                      excluded_for_data_quality. False = keep them, flagged.

    peer_relative: add each result's P/E and ROCE vs its industry median
                   (first 20 results; slower — one industry fetch per industry).

    How it runs: fundamental clauses go to Screener.in (login required);
    technical clauses are then checked against up to `max_candidates` of those
    matches. With only technical clauses, candidates come from a public NSE
    index — `universe` (default "nifty500"; also nifty50, midcap100,
    smallcap100, smallcap250, bank, it, auto, pharma, fmcg, metal, realty,
    energy, defence, chemicals, ...) — so no login is needed.

    sort_by: a technical metric (rsi14, pct_above_52w_low, volume_vs_20d_avg, ...)
             or a result column name. Default: the first technical filter.

    Returns partial=true (with a reason) if only some candidates could be
    checked — e.g. more fundamental matches than max_candidates.
    """
    return await _safe(_screen)(
        query, sort_by=sort_by, order=order, limit=limit, universe=universe,
        max_candidates=max_candidates, min_market_cap=min_market_cap,
        exclude_flagged=exclude_flagged, peer_relative=peer_relative,
    )


@mcp.tool(annotations={"title": "Relative Valuation", "readOnlyHint": True, "openWorldHint": True})
async def get_relative_valuation(symbols: list[str] | str) -> dict:
    """
    Is this P/E cheap *for its industry*? Compares each stock's P/E and ROCE
    with the median of every listed company in its Screener industry, for up
    to 20 stocks in one call (stocks in the same industry share one fetch, so
    this scales across a screen's results).

    Returns per stock: industry, P/E vs industry median (%), ROCE vs median
    (pp), and a one-line assessment (e.g. "at a premium to its industry, but
    the premium comes with clearly higher ROCE", or a possible value trap).
    Sorted cheapest-vs-industry first.

    symbols: list (["HBLENGINE", "BSE", "TCS"]) or comma-separated string

    Tip: screen_stocks(..., peer_relative=True) attaches the same data to
    screen results directly.
    """
    return await _safe(_get_relative_valuation)(symbols)


@mcp.tool(annotations={"title": "Moat Signals", "readOnlyHint": True, "openWorldHint": True})
async def get_moat_signals(symbol: str) -> dict:
    """
    Quantitative moat proxies for a company — market position plus how
    durable its economics have been.

    Market position (from every listed company in its Screener industry):
      revenue share and rank by quarterly sales, industry concentration (HHI
      and CR4 — fragmented / moderately / highly concentrated), market-leader
      and top-3 flags.
    Durability (from up to ~12 years of statements):
      ROCE consistency (years ≥ 15%, median, min), operating-margin stability
      (standard deviation), sales CAGR, promoter-holding stability.
    Plus a signals summary with the thresholds used.

    These are proxies — listed-company revenue share misses unlisted and
    imported competitors, and brand / switching costs / licences still need
    judgement (check the annual report and earnings calls).

    symbol: NSE/BSE symbol or company name
    """
    return await _safe(_get_moat_signals)(symbol)


@mcp.tool(annotations={"title": "Forward Outlook", "readOnlyHint": True, "openWorldHint": True})
async def get_forward_outlook(symbol: str, days: int = 180, include_earnings_call: bool = True) -> dict:
    """
    Forward-looking inputs in one call, instead of extrapolating trailing data:

      1. Analyst estimates — current and next fiscal-year EPS and revenue
         consensus (low/high, growth, analyst count) and forward P/E.
      2. Order wins — NSE filings in the last `days` announcing orders,
         contracts, LoAs, with rupee values stated in the headline summed.
      3. Capex & expansion — filings about new capacity, plants, commissioning.
      4. Management guidance — the most relevant passages from the latest
         earnings call on guidance, order book / pipeline, and capex
         (needs the [ai] extra; otherwise returns an install hint).

    Each part is independent — if one source is down the result is partial.

    symbol: NSE/BSE symbol or company name
    days: filing look-back (30-730, default 180)
    include_earnings_call: set False to skip the transcript step (faster)
    """
    return await _safe(_get_forward_outlook)(symbol, days, include_earnings_call)


@mcp.tool(annotations={"title": "52-Week-Low Candidates", "readOnlyHint": True, "openWorldHint": True})
async def get_52_week_low_candidates(
    min_roce: float = 15,
    max_debt_to_equity: float = 0.5,
    max_pct_above_52w_low: float = 10,
    min_market_cap: float = 1000,
    universe: str = "",
    limit: int = 20,
) -> dict:
    """
    Quality stocks trading near their 52-week low — in one call.

    Combines quality filters (ROCE, debt-to-equity, market cap in ₹ Cr) with
    proximity to the 52-week low, and returns for each match: current price,
    52W high/low, % above the low, RSI/DMA context, and the same clean fields
    as get_company_overview (P/E, ROCE, ROE, book value, dividend yield,
    debt-to-equity).

    With a Screener login and no `universe`, scans the whole market via a
    Screener query. Otherwise scans an index's constituents (default Nifty
    500) and computes debt-to-equity from each match's latest balance sheet.

    Examples:
      get_52_week_low_candidates()
      get_52_week_low_candidates(min_roce=20, max_debt_to_equity=0.2, max_pct_above_52w_low=5)
      get_52_week_low_candidates(universe="midcap100", min_market_cap=5000)
    """
    return await _safe(_get_52w_low)(
        min_roce, max_debt_to_equity, max_pct_above_52w_low, min_market_cap, universe, limit,
    )


@mcp.tool(annotations={"title": "Compare To Sector", "readOnlyHint": True, "openWorldHint": True})
async def compare_to_sector(symbol: str, days: int = 30, benchmark: str = "") -> dict:
    """
    Has this stock fallen (or risen) more or less than its sector and the
    market? Separates a market/sector pullback from a company-specific move.

    Compares the stock's close-to-close return against its sector index
    (auto-picked from its Screener sector, e.g. Nifty Auto, Nifty IT, Nifty
    India Defence) and the Nifty 50, over `days` calendar days plus 7/30/90-day
    context windows. Also returns beta and correlation vs the sector, drawdown
    from the window high, and a plain-language verdict.

    symbol: NSE/BSE symbol
    days: window in calendar days (5-365, default 30)
    benchmark: optional index override — nifty50, nifty500, bank, it, auto,
               pharma, fmcg, metal, realty, energy, defence, chemicals, ...

    Examples:
      compare_to_sector("MSUMI", days=60)
      compare_to_sector("HDFCBANK", benchmark="private_bank")
    """
    return await _safe(_compare_to_sector)(symbol, days, benchmark)


@mcp.tool(annotations={"title": "Get Recent News", "readOnlyHint": True, "openWorldHint": True})
async def get_recent_news(symbol: str, days: int = 14, limit: int = 20) -> dict:
    """
    Recent news headlines about a company (Google News, Indian edition).

    Returns publisher, publish time (UTC) and link for each headline, newest
    first, de-duplicated. Use it for the "what's happened lately" context that
    financial statements can't show — results reactions, brokerage calls,
    management moves, price hikes, regulatory news. For the company's own
    exchange filings use get_company_announcements instead.

    symbol: NSE/BSE symbol or company name
    days: look-back window (1-90, default 14)
    limit: max headlines (default 20)

    Examples:
      get_recent_news("TMPV")
      get_recent_news("Garden Reach", days=30)
    """
    return await _safe(_get_recent_news)(symbol, days, limit)


@mcp.tool(annotations={"title": "Get Analyst Targets", "readOnlyHint": True, "openWorldHint": True})
async def get_analyst_targets(symbol: str) -> dict:
    """
    Analyst price targets for a stock, from two independent sources:

    1. Consensus (Yahoo Finance): mean / median / high / low target, number
       of analysts, implied upside vs the current price, and the
       strong-buy/buy/hold/sell/strong-sell split.
    2. Broker targets mentioned in the last 60 days of news headlines
       (e.g. "ICICI Securities target ₹370"), with links to each article.

    The two come from different broker sets and dates, so they rarely match;
    both are labelled with their source. Returns partial=true if one source is
    unavailable, or if the stock has no analyst coverage.

    symbol: NSE/BSE symbol or company name

    Examples:
      get_analyst_targets("TMPV")
      get_analyst_targets("HDFCBANK")
    """
    return await _safe(_get_analyst_targets)(symbol)


@mcp.tool(annotations={"title": "Screen By Theme", "readOnlyHint": True, "openWorldHint": True})
async def screen_by_theme(theme: str, limit: int = 20) -> dict:
    """
    Run a pre-built thematic stock screen.

    Available themes:
      undervalued_small_cap   — Small caps with ROCE > 15%, low debt, PE < 20
      high_roce_low_debt      — ROCE > 20%, debt to equity < 0.3
      compounders             — 15%+ growth across revenue, profit, ROE, ROCE
      turnaround              — Companies with strong recent profit recovery
      rising_profit_falling_price — Profit up, price compressed (potential value)
      improving_roce          — ROCE > 15% with profit momentum
      hidden_gems             — Small cap, high ROCE, strong growth
      dividend_aristocrats    — Consistent dividend payers with strong financials
      qarp                    — Quality at reasonable price
      micro_cap_growth        — High-growth micro caps < ₹1000 Cr
      ev_theme                — EV & auto ancillary growth companies
      chemicals               — Specialty chemicals with strong fundamentals
      defense                 — Defense sector companies with revenue momentum
      railways                — Railway infra/equipment companies
      renewable_energy        — Renewable energy sector growth companies

    Examples:
      screen_by_theme("hidden_gems")
      screen_by_theme("compounders")
      screen_by_theme("chemicals")
    """
    return await _safe(_theme)(theme, limit=limit)


@mcp.tool(annotations={"title": "List Investment Themes", "readOnlyHint": True, "openWorldHint": False})
async def list_investment_themes() -> dict:
    """
    List all available pre-built investment themes with their screening criteria.
    Use this to discover what thematic screens are available.
    """
    return await _safe(_list_themes)()


# ─── Company Deep Dive ─────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Get Company Overview", "readOnlyHint": True, "openWorldHint": True})
async def get_company_overview(symbol: str, financial_type: str = "consolidated") -> dict:
    """
    Get a company's key ratios, current price, 52-week range, and about section.

    data.price_freshness says when the price is from (price_as_of), whether
    it's an intraday print or the last close, the previous close and the
    day's change — so you can tell how current "current_price" is.

    symbol: NSE/BSE symbol (e.g., "TCS", "INFY", "RELIANCE", "HDFCBANK")
    financial_type: "consolidated" (default) or "standalone"

    This is the best starting point for any company analysis.
    """
    return await _safe(_get_overview)(symbol, financial_type)


@mcp.tool(annotations={"title": "Get Financials", "readOnlyHint": True, "openWorldHint": True})
async def get_financials(
    symbol: str,
    statement: str = "profit_loss",
    financial_type: str = "consolidated",
    years: int = 5,
) -> dict:
    """
    Get financial statements for a company.

    symbol: NSE/BSE symbol
    statement options:
      "profit_loss"    — Revenue, expenses, EBITDA, PAT (default)
      "balance_sheet"  — Assets, liabilities, equity, debt
      "cash_flow"      — Operating, investing, financing cash flows
      "ratios"         — Historical PE, PB, ROCE, ROE, etc.
    financial_type: "consolidated" or "standalone"
    years: number of years to show (default 5, max 10)
    """
    valid = {"profit_loss", "balance_sheet", "cash_flow", "ratios"}
    if statement not in valid:
        return error_envelope(f"Invalid statement type '{statement}'. Choose from: {', '.join(sorted(valid))}", "invalid_input")
    return await _safe(_get_financials)(symbol, statement, financial_type, years)


@mcp.tool(annotations={"title": "Get Quarterly Results", "readOnlyHint": True, "openWorldHint": True})
async def get_quarterly_results(symbol: str, financial_type: str = "consolidated") -> dict:
    """
    Get the last 8 quarters of results for a company.

    Shows: Revenue, Expenses, Operating Profit, OPM%, Net Profit, EPS
    Useful for identifying quarter-on-quarter growth trends and seasonality.

    symbol: NSE/BSE symbol (e.g., "TCS", "WIPRO", "MARUTI")
    """
    return await _safe(_get_quarterly)(symbol, financial_type)


@mcp.tool(annotations={"title": "Get Shareholding Pattern", "readOnlyHint": True, "openWorldHint": True})
async def get_shareholding_pattern(symbol: str) -> dict:
    """
    Get shareholding pattern history for a company (last 8 quarters).

    Shows: Promoter, FII, DII, Public holding percentages + pledged %
    Also provides a trend analysis of promoter holding changes.

    Useful for:
      - Detecting promoter confidence (buying/selling)
      - Monitoring FII/DII interest
      - Flagging pledge concerns

    symbol: NSE/BSE symbol
    """
    return await _safe(_get_shareholding)(symbol)


@mcp.tool(annotations={"title": "Get Peer Comparison", "readOnlyHint": True, "openWorldHint": True})
async def get_peer_comparison(symbol: str, financial_type: str = "consolidated") -> dict:
    """
    Get peer comparison table as shown on Screener.in for a company.

    Shows the company alongside its sector peers with key metrics
    like Market Cap, Sales, Net Profit, PE, ROCE, etc.

    symbol: NSE/BSE symbol
    """
    return await _safe(_get_peers)(symbol, financial_type)


@mcp.tool(annotations={"title": "Compare Companies", "readOnlyHint": True, "openWorldHint": True})
async def compare_companies(symbols: list[str], financial_type: str = "consolidated") -> dict:
    """
    Side-by-side comparison of 2 to 5 companies on all key ratios.

    Fetches data for each company and presents them in a comparative table.
    Best for "ITC vs HUL vs Nestle" type questions.

    symbols: list of NSE/BSE symbols, e.g., ["ITC", "HINDUNILVR", "NESTLEIND"]
    financial_type: "consolidated" or "standalone"

    Examples:
      compare_companies(["ITC", "HINDUNILVR"])
      compare_companies(["TCS", "INFY", "WIPRO", "HCLTECH"])
      compare_companies(["PIDILITIND", "ASIANPAINT", "BERGEPAINT"])
    """
    return await _safe(_compare)(symbols, financial_type)


# ─── Analysis Tools ────────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Get Full Analysis", "readOnlyHint": True, "openWorldHint": True})
async def get_full_analysis(symbol: str, financial_type: str = "consolidated") -> dict:
    """
    Fetch ALL financial data for a company in a single call.

    Returns complete data across:
      - Key ratios and overview
      - 10-year P&L, Balance Sheet, Cash Flow
      - 8 quarters of results
      - Historical ratios (PE, ROCE, ROE, etc.)
      - Shareholding pattern
      - Peer comparison

    Use this when you need to do a thorough analysis, identify trends,
    explain a company in depth, or answer complex multi-part questions.

    symbol: NSE/BSE symbol
    """
    return await _safe(_full_analysis)(symbol, financial_type)


@mcp.tool(annotations={"title": "Analyze Red Flags", "readOnlyHint": True, "openWorldHint": True})
async def analyze_red_flags(symbol: str, financial_type: str = "consolidated") -> dict:
    """
    Fetch all financial data for a company and generate a structured red flag analysis.

    Systematically checks for:
      - Declining promoter holding or high pledging
      - Rising debt trends
      - Falling ROCE/ROE
      - Cash flow vs profit divergence (profit without cash = concern)
      - Revenue growth without profit growth
      - Rising receivables or inventory vs sales

    Returns data + analysis framework for Claude to identify and explain red flags.

    symbol: NSE/BSE symbol
    """
    return await _safe(_red_flags)(symbol, financial_type)


@mcp.tool(annotations={"title": "Explain For Beginners", "readOnlyHint": True, "openWorldHint": True})
async def explain_for_beginners(symbol: str) -> dict:
    """
    Explain a company in simple, beginner-friendly language.

    Fetches all data and produces a plain-English explanation:
      - What does this company do and how does it make money?
      - Is it profitable and growing?
      - What do the key numbers mean in everyday language?
      - Is the stock expensive or cheap right now?
      - What should a first-time investor watch out for?

    Perfect for: "Explain Jyothy Labs like I'm a beginner"

    symbol: NSE/BSE symbol
    """
    return await _safe(_beginner)(symbol)


def _find_ratio(ratios: dict[str, str], *needles: str) -> str:
    """Find a key_ratios value by fuzzy substring match (Screener's exact labels vary)."""
    for needle in needles:
        for key, value in ratios.items():
            if needle.lower() in key.lower():
                return value
    return ""


def _num(text: str) -> float | None:
    """Parse a Screener-formatted number like '₹ 1,23,456 Cr.' or '23.4%' into a float."""
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def _compute_red_flags(ratios: dict[str, str], financial: bool = False) -> list[dict[str, str]]:
    """Deterministic, rule-based red flag checks computed from a single snapshot of ratios.

    Not a substitute for the LLM-driven analyze_red_flags tool (which reasons over
    full history) — this only checks point-in-time thresholds so the dashboard has
    something real to render without an extra model round-trip.
    """
    flags: list[dict[str, str]] = []

    debt_equity = None if financial else _num(_find_ratio(ratios, "Debt to equity"))
    if debt_equity is not None:
        if debt_equity > 1.5:
            flags.append({"flag": f"High debt-to-equity ratio ({debt_equity:.2f})", "severity": "critical"})
        elif debt_equity > 0.8:
            flags.append({"flag": f"Elevated debt-to-equity ratio ({debt_equity:.2f})", "severity": "warning"})

    roce = None if financial else _num(_find_ratio(ratios, "Return on capital employed", "ROCE"))
    if roce is not None and roce < 10:
        flags.append({"flag": f"Low return on capital employed ({roce:.1f}%)", "severity": "warning"})

    roe = _num(_find_ratio(ratios, "Return on equity", "ROE"))
    if roe is not None and roe < 10:
        flags.append({"flag": f"Low return on equity ({roe:.1f}%)", "severity": "warning"})

    pe = _num(_find_ratio(ratios, "Stock P/E", "P/E"))
    if pe is not None and pe > 60:
        flags.append({"flag": f"Very high valuation — P/E of {pe:.1f}", "severity": "info"})

    return flags


@mcp.tool(
    annotations={"title": "Compare Stocks (Interactive UI)", "readOnlyHint": True, "openWorldHint": True},
    # "ui/resourceUri" is the older flat key some hosts still read
    meta={"ui": {"resourceUri": STOCK_COMPARISON_WIDGET_URI}, "ui/resourceUri": STOCK_COMPARISON_WIDGET_URI},
)
async def compare_stocks_ui(symbols: list[str] | str) -> dict:
    """
    Interactive stock comparison dashboard.

    Compare multiple stocks side-by-side with real-time price, market cap,
    P/E, ROE, ROCE, debt-to-equity, dividend yield and rule-based red flag
    checks. Opens an interactive dashboard (MCP App) in hosts that support
    it — best value in each row highlighted, one-click retry for ambiguous
    symbols; other hosts get the same data as JSON.

    Args:
        symbols: Stock symbols, either as a list (["TCS", "INFY", "WIPRO"])
            or a comma-separated string ("TCS,INFY,WIPRO")

    Examples:
      compare_stocks_ui(["TCS", "INFY", "WIPRO"])
      compare_stocks_ui(["HDFCBANK", "ICICIBANK", "AXISBANK"])
      compare_stocks_ui("HINDUNILVR,ITC,NESTLEIND")

    Company names and near-miss symbols are resolved like everywhere else.
    """
    env = await _safe(_compare_stocks_ui_impl)(symbols)
    # The dashboard UI predates the envelope and reads `stocks` / `count` at
    # the top level — mirror them there so it keeps rendering.
    data = env.get("data") or {}
    return {**env, "stocks": data.get("stocks", []), "count": data.get("count", 0)}


async def _compare_stocks_ui_impl(symbols: list[str] | str) -> ToolResult:
    import asyncio
    from .core.company_page import fetch_company_page
    from .core.quality import is_financial, overview_missing_fields
    from .parsers.company import debt_to_equity, parse_overview

    if isinstance(symbols, str):
        symbols = symbols.split(",")
    stock_list = [s.strip() for s in symbols if s.strip()][:6]

    async def fetch(sym: str):
        page = await fetch_company_page(sym, "consolidated")
        return page, parse_overview(page.html)

    results = await asyncio.gather(*[fetch(s) for s in stock_list], return_exceptions=True)

    stocks = []
    warnings: list[str] = []
    missing_all: list[str] = []
    for sym, result in zip(stock_list, results):
        if isinstance(result, Exception):
            entry = {"symbol": sym.upper(), "error": str(result)}
            if isinstance(result, ToolError) and result.details.get("candidates"):
                entry["candidates"] = result.details["candidates"]
            stocks.append(entry)
            warnings.append(f"{sym.upper()}: {result}")
            continue

        page, overview = result
        warnings += page.warnings
        missing = overview_missing_fields(overview)
        missing_all += [f"{page.symbol}.{m}" for m in missing]
        ratios = overview.get("key_ratios", {})
        financial = is_financial(overview.get("sectors", []))
        stocks.append({
            "symbol": page.symbol,
            "name": overview.get("name"),
            "financial_type": page.financial_type,
            "price": overview.get("current_price") or None,
            "market_cap": _find_ratio(ratios, "Market Cap") or None,
            "pe_ratio": _find_ratio(ratios, "Stock P/E", "P/E") or None,
            "roe": _find_ratio(ratios, "Return on equity", "ROE") or None,
            "roce": _find_ratio(ratios, "Return on capital employed", "ROCE") or None,
            "debt_to_equity": None if financial else debt_to_equity(page.html),
            "dividend_yield": _find_ratio(ratios, "Dividend Yield") or None,
            "book_value": _find_ratio(ratios, "Book Value") or None,
            "missing_fields": missing,
            "red_flags": _compute_red_flags(
                {**ratios, "Debt to equity": str(debt_to_equity(page.html) or "")}, financial=financial),
        })

    failed = [s for s in stocks if "error" in s]
    return ToolResult(
        data={"stocks": stocks, "count": len(stocks) - len(failed)},
        warnings=warnings,
        missing_fields=missing_all,
        partial=bool(failed or missing_all),
        reason="Some symbols failed or had blank fields on the source page." if (failed or missing_all) else None,
    )


# ─── Document Analysis ────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Get Document List", "readOnlyHint": True, "openWorldHint": True})
async def get_document_list(symbol: str) -> dict:
    """
    List all available annual reports and earnings call transcripts for a company.

    Fetches from Screener.in company page and NSE API.

    symbol: NSE/BSE symbol (e.g., "TCS", "INFY")

    Use this before calling analyze_annual_report or analyze_earnings_call
    to see what documents are available and their years/quarters.
    """
    return await _safe(_get_document_list)(symbol)


@mcp.tool(annotations={"title": "Analyze Annual Report", "readOnlyHint": True, "openWorldHint": True})
async def analyze_annual_report(
    symbol: str,
    year: int,
    question: str = "",
    pdf_url: str = "",
    force_reindex: bool = False,
) -> dict:
    """
    Ask any question about a company's annual report using AI-powered semantic search.

    Downloads the PDF, indexes it into a local vector database (ChromaDB),
    and retrieves the most relevant sections to answer your question.
    Results are cached — subsequent calls on the same report are instant.
    The response's data.document.freshness shows last_indexed_at, the
    source PDF's sha256/ETag, and source_changed (a HEAD check against the
    live PDF; null if it can't tell) so you can judge staleness.

    symbol: NSE/BSE symbol (e.g., "TCS")
    year: report year (e.g., 2024, 2023)
    question: what you want to know (leave blank for a general summary)
    pdf_url: optional — provide directly if you have the link
    force_reindex: re-download the PDF and rebuild the index (use when
                   source_changed is true or the index is old)

    Requires: pip install pdfplumber sentence-transformers chromadb

    Examples:
      analyze_annual_report("TCS", 2024, "What are the key risks mentioned?")
      analyze_annual_report("INFY", 2023, "What did management say about margins?")
      analyze_annual_report("RELIANCE", 2024, "Summarize the new energy segment")
    """
    return await _safe(_analyze_annual_report)(symbol, year, question, pdf_url or None, force_reindex)


@mcp.tool(annotations={"title": "Analyze Earnings Call", "readOnlyHint": True, "openWorldHint": True})
async def analyze_earnings_call(
    symbol: str,
    quarter: str,
    question: str = "",
    pdf_url: str = "",
    force_reindex: bool = False,
) -> dict:
    """
    Ask any question about an earnings call transcript using semantic search.

    Same RAG pipeline as analyze_annual_report — downloads, indexes, and retrieves
    relevant sections from the transcript PDF.

    symbol: NSE/BSE symbol
    quarter: e.g., "Q1FY25", "Q2FY26", "Q3FY25"
    question: what you want to know (leave blank for a management commentary summary)
    pdf_url: optional — provide directly if you have the link
    force_reindex: re-download and rebuild the cached index (see
                   data.document.freshness for last_indexed_at / source_changed)

    Requires: pip install pdfplumber sentence-transformers chromadb

    Examples:
      analyze_earnings_call("HDFCBANK", "Q3FY25", "What is the guidance on NIM?")
      analyze_earnings_call("TCS", "Q2FY25", "What did they say about deal wins?")
    """
    return await _safe(_analyze_earnings_call)(symbol, quarter, question, pdf_url or None, force_reindex)


@mcp.tool(annotations={"title": "Ask Company Research", "readOnlyHint": True, "openWorldHint": True})
async def ask_company_research(
    symbol: str,
    question: str,
    max_annual_reports: int = 3,
    max_earnings_calls: int = 4,
) -> dict:
    """
    Ask a question across ALL of a company's cached documents at once —
    multiple annual reports AND earnings call transcripts together — instead
    of picking one document at a time like analyze_annual_report/analyze_earnings_call.

    Best for cross-year or cross-quarter questions that a single document can't
    answer, e.g. "how has capex strategy evolved over the last 3 years?" or
    "has management's tone on margins changed across recent quarters?"

    Indexes (or reuses cached indexes for) the most recent `max_annual_reports`
    annual reports and `max_earnings_calls` earnings calls, then runs one
    semantic search across all of them, ranked by relevance.

    Requires: pip install pdfplumber sentence-transformers chromadb

    Examples:
      ask_company_research("TCS", "How has capex strategy evolved over the last 3 years?")
      ask_company_research("HDFCBANK", "Has management's tone on NIM changed across recent quarters?", max_annual_reports=2)
    """
    return await _safe(_ask_company_research)(
        symbol, question, max_annual_reports, max_earnings_calls
    )


@mcp.tool(annotations={"title": "Search Market Commentary", "readOnlyHint": True, "openWorldHint": True})
async def search_market_commentary(
    question: str,
    symbols: list[str],
    top_k_per_symbol: int = 3,
) -> dict:
    """
    Semantic search for a question across MULTIPLE companies' already-indexed
    documents at once — e.g. "which of these companies mentioned raw material
    cost pressure in their recent earnings calls?"

    Only searches documents already indexed via analyze_annual_report,
    analyze_earnings_call, or ask_company_research for each symbol — it does
    NOT download new documents, so cost/latency stays bounded no matter how
    many symbols are passed. Run ask_company_research(symbol, ...) first for
    any symbol you want included that hasn't been indexed yet.

    Requires: pip install pdfplumber sentence-transformers chromadb

    Examples:
      search_market_commentary("raw material cost pressure", ["TATASTEEL", "JSWSTEEL", "SAIL"])
      search_market_commentary("management outlook on margins", ["ITC", "HINDUNILVR", "NESTLEIND"])
    """
    return await _safe(_search_market_commentary)(question, symbols, True, top_k_per_symbol)


# ─── Corporate Actions & Events ────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Get Company Announcements", "readOnlyHint": True, "openWorldHint": True})
async def get_company_announcements(
    symbol: str,
    category: str = "all",
    days: int = 30,
) -> dict:
    """
    Fetch recent company announcements from NSE.

    symbol: NSE trading symbol (e.g., "TCS", "RELIANCE")
    category: filter by type — "all" | "results" | "board_meeting" | "dividend"
              | "insider_trading" | "agm" | "acquisition" | "buyback" | "fund_raise"
    days: how many days to look back (default 30, max 365)

    Examples:
      get_company_announcements("INFY", "results", 90)
      get_company_announcements("HDFCBANK", "dividend")
      get_company_announcements("RELIANCE", "all", 7)
    """
    return await _safe(_get_announcements)(symbol, category, days)


@mcp.tool(annotations={"title": "Search Shareholder", "readOnlyHint": True, "openWorldHint": True})
async def search_shareholder(
    name: str,
    symbol: str = "",
    days: int = 365,
) -> dict:
    """
    Search NSE bulk/block deals to find activity by a specific investor or entity.

    Useful for tracking: FIIs, mutual funds, promoters, known investors.

    name: partial or full name (e.g., "Jhunjhunwala", "SBI Mutual Fund", "HDFC AMC")
    symbol: optional — restrict search to one company's deals
    days: how many days of history to search (default 365)

    Note: Only captures NSE bulk deals (single trade > 0.5% of equity).
    For aggregate FII/DII/Promoter holdings, use get_shareholding_pattern().

    Examples:
      search_shareholder("Jhunjhunwala")
      search_shareholder("SBI Mutual Fund", symbol="TCS")
      search_shareholder("Nalanda Capital", days=730)
    """
    return await _safe(_search_shareholder)(name, symbol or None, days)


@mcp.tool(annotations={"title": "Get Bulk Deals", "readOnlyHint": True, "openWorldHint": True})
async def get_bulk_deals(symbol: str, days: int = 90) -> dict:
    """
    Fetch all NSE bulk deals for one company — no investor name required.

    Unlike search_shareholder (which needs a name to filter by), this returns
    every bulk deal (>0.5% of equity in a single trade) recorded for the symbol,
    useful for "who's been trading large blocks of X" style questions.

    symbol: NSE trading symbol (e.g., "RELIANCE")
    days: how many days of history to search (default 90, max 365)

    Examples:
      get_bulk_deals("YESBANK")
      get_bulk_deals("ADANIENT", days=180)
    """
    return await _safe(_get_bulk_deals)(symbol, days)


@mcp.tool(annotations={"title": "Get Insider Trading", "readOnlyHint": True, "openWorldHint": True})
async def get_insider_trading(symbol: str) -> dict:
    """
    Recent insider trading disclosures (SEBI PIT Regulation 7(2)) for a company.

    Shows promoter/KMP/designated-person trades — buy or sell, quantity,
    value, and holding before/after — with no minimum trade size. This is
    different from `get_bulk_deals`/`search_shareholder`, which only catch
    single trades over 0.5% of equity and so miss most insider activity.

    symbol: NSE trading symbol (e.g., "RELIANCE", "INFY")

    Examples:
      get_insider_trading("RELIANCE")
      get_insider_trading("ADANIENT")
    """
    return await _safe(_get_insider_trading)(symbol)


@mcp.tool(annotations={"title": "Get Promoter Pledge History", "readOnlyHint": True, "openWorldHint": True})
async def get_promoter_pledge_history(symbol: str) -> dict:
    """
    Dedicated promoter pledge % trend for a company, with severity assessment.

    Pulls the pledge row out of the shareholding table (if one exists) and
    flags severity: >50% pledged = high risk, 20-50% = moderate, <20% = low,
    none = clean.

    symbol: NSE/BSE symbol

    Examples:
      get_promoter_pledge_history("ZEEL")
      get_promoter_pledge_history("RELIANCE")
    """
    return await _safe(_get_pledge_history)(symbol)


@mcp.tool(annotations={"title": "Get Credit Ratings", "readOnlyHint": True, "openWorldHint": True})
async def get_credit_ratings(symbol: str, days: int = 730) -> dict:
    """
    Credit rating actions (CRISIL/ICRA/CARE/India Ratings) for a company —
    a governance/debt-quality check for long-term holders, alongside
    `get_promoter_pledge_history`.

    symbol: NSE trading symbol (e.g., "TCS", "RELIANCE")
    days: look back this many days (default 730 — rating actions are
          infrequent, often just 1-2 per year)

    Examples:
      get_credit_ratings("RELIANCE")
      get_credit_ratings("ADANIENT", days=365)
    """
    return await _safe(_get_credit_ratings)(symbol, days)


# ─── Commodity Analysis ────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Get Commodity Prices", "readOnlyHint": True, "openWorldHint": True})
async def get_commodity_prices(commodity: str, years: int = 5) -> dict:
    """
    Get commodity price context and its impact on Indian listed companies.

    Returns the international benchmark price the MCX contract tracks
    (COMEX gold/silver/copper, Brent, Henry Hub, etc.), with 4-week, 52-week
    and `years`-period moves and range, USD/INR, and an approximate INR price
    (a pure FX conversion — excludes import duty/GST, so below MCX). Also covers
    which companies benefit or suffer from price moves, and Screener queries to
    find exposed companies.

    commodity: gold | silver | crude_oil | copper | aluminium | zinc | nickel
               | cotton | natural_gas | steel   (nickel: no free feed → partial)
    years: history period for the moves and range (1–10, default 5)

    Examples:
      get_commodity_prices("crude_oil")
      get_commodity_prices("copper", years=3)
      get_commodity_prices("gold")
    """
    return await _safe(_get_commodity_prices)(commodity, years)


# ─── Research Notebook ────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Notebook AI (Research Notes)", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
async def notebook_ai(
    action: str,
    symbol: str = "",
    content: str = "",
    note_id: str = "",
) -> dict:
    """
    Save, read, and AI-summarize your investment research notes locally.

    Notes are stored in ~/.screener-mcp/notebooks/ — persists across sessions.

    action:
      "create"    — new note (requires symbol + content)
      "append"    — add to existing note (requires note_id + content)
      "read"      — read a note (requires note_id)
      "list"      — list all notes (optional symbol filter)
      "summarize" — AI-structured summary of all notes for a symbol
      "delete"    — delete a note (requires note_id)

    Examples:
      notebook_ai("create", symbol="TCS", content="Q3 results strong — revenue beat...")
      notebook_ai("append", note_id="a1b2c3d4", content="Met management — bullish on BFSI...")
      notebook_ai("list", symbol="TCS")
      notebook_ai("summarize", symbol="TCS")
      notebook_ai("read", note_id="a1b2c3d4")
    """
    return await _safe(_notebook_ai)(action, symbol or None, content or None, note_id or None)


# ─── Portfolio ──────────────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Add Portfolio Stock", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
async def add_portfolio_stock(symbol: str, quantity: float, avg_price: float) -> dict:
    """
    Add a holding to your local portfolio (~/.screener-mcp/portfolio.json).

    If you already hold this symbol, the new lot merges into the existing
    position with a quantity-weighted average price — like a real broker
    ledger, buying more at a different price updates your average cost
    rather than overwriting it.

    symbol: NSE/BSE symbol (e.g., "RELIANCE")
    quantity: number of shares in this lot
    avg_price: price per share for this lot

    Examples:
      add_portfolio_stock("RELIANCE", quantity=10, avg_price=1350)
      add_portfolio_stock("TCS", quantity=5, avg_price=3800)
    """
    return await _safe(_add_portfolio_stock)(symbol, quantity, avg_price)


@mcp.tool(annotations={"title": "Update Portfolio Stock", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def update_portfolio_stock(
    symbol: str,
    quantity: float = 0.0,
    avg_price: float = 0.0,
) -> dict:
    """
    Overwrite quantity and/or average price for an existing holding — e.g.
    after a partial sell (set the new remaining quantity) or to correct
    your recorded cost basis.

    Use `add_portfolio_stock` instead for a new buy lot — it recalculates
    the average price for you rather than replacing it.

    symbol: NSE/BSE symbol already in your portfolio
    quantity: new total share count (0 = leave unchanged)
    avg_price: new average price (0 = leave unchanged)

    Examples:
      update_portfolio_stock("RELIANCE", quantity=5)   # sold half
      update_portfolio_stock("TCS", avg_price=3750)     # correct cost basis
    """
    return await _safe(_update_portfolio_stock)(
        symbol,
        quantity if quantity else None,
        avg_price if avg_price else None,
    )


@mcp.tool(annotations={"title": "Remove Portfolio Stock", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
async def remove_portfolio_stock(symbol: str) -> dict:
    """
    Remove a holding entirely from the portfolio (full exit).

    Examples:
      remove_portfolio_stock("YESBANK")
    """
    return await _safe(_remove_portfolio_stock)(symbol)


@mcp.tool(annotations={"title": "Get Portfolio", "readOnlyHint": True, "openWorldHint": True})
async def get_portfolio() -> dict:
    """
    View your portfolio with live prices, P&L, and per-holding weight.

    Fetches the current price for each holding from Screener.in and computes
    invested value, current value, gain/loss (₹ and %), and each position's
    weight in the total portfolio.

    Examples:
      get_portfolio()
    """
    return await _safe(_get_portfolio)()


# ─── Resources ────────────────────────────────────────────────────────────────

@mcp.resource("screener://analyst-guide")
def analyst_guide() -> str:
    """How to use the Screener Stock Research assistant."""
    return """
# Screener Stock Research — Analyst Guide

## What I can do

I'm your Indian stock research copilot powered by Screener.in data.

### Ask me naturally:
- "Find chemical stocks with low debt and strong growth"
- "Compare ITC and HUL"
- "Explain Jyothy Labs like I'm a beginner"
- "What are the red flags in Asian Paints?"
- "Find hidden gems below ₹5000 crore market cap"
- "Which companies benefit from EV adoption?"
- "Give me businesses with improving ROCE for the last 5 years"
- "Find turnaround stories in mid-cap space"
- "What is Titan's promoter holding trend?"
- "Show me the last 4 quarters for HDFC Bank"

### What I fetch from Screener.in:
- 10+ years of financials (P&L, Balance Sheet, Cash Flow)
- Historical key ratios (PE, ROCE, ROE, Debt/Equity, etc.)
- Quarterly results (last 8 quarters)
- Shareholding patterns (promoter, FII, DII)
- Peer comparison tables
- Stock screening by custom filters or pre-built themes

### Setup for full data:
Set in your environment:
  SCREENER_USERNAME=your@email.com
  SCREENER_PASSWORD=yourpassword

Without login, some data fields may be restricted (Screener.in requires login for full data).

## Limitations
- Data comes from Screener.in and may lag by 1 quarter
- I cannot predict stock prices or guarantee returns
- Always verify critical data directly on Screener.in
- Past financial performance does not guarantee future results
"""


@mcp.resource("screener://query-syntax")
def query_syntax_guide() -> str:
    """Screener.in query language reference for stock screening."""
    return """
# Screener.in Query Language Reference

## Syntax
  FIELD OPERATOR VALUE [AND FIELD OPERATOR VALUE ...]

## Operators
  >   greater than
  <   less than
  =   equals
  AND combine conditions

## Common Fields (exact spelling)
  Market Capitalization          (₹ Crore)
  Current Price
  Price to Earning               (PE ratio)
  Price to book value            (PB ratio)
  EV / EBITDA
  PEG Ratio
  Dividend yield                 (%)
  Return on capital employed     (%)
  Return on equity               (%)
  Debt to equity
  Current ratio
  Pledged percentage             (%)
  Sales growth 5Years            (%)
  Sales growth 3Years            (%)
  Sales growth last year         (%)
  Profit growth 5Years           (%)
  Profit growth 3Years           (%)
  Profit growth last year        (%)
  Average return on equity 5Years
  Average return on capital employed 5Years
  Net cash flow last year

## Example Queries

# Classic GARP (Growth at Reasonable Price)
Price to Earning < 25 AND Profit growth 5Years > 15 AND Return on equity > 15

# Deep Value Small Cap
Market Capitalization < 2000 AND Price to Earning < 15 AND Debt to equity < 0.5

# Quality Compounder
Sales growth 5Years > 15 AND Profit growth 5Years > 15 AND Return on capital employed > 20 AND Debt to equity < 0.3

# High Dividend + Quality
Dividend yield > 3 AND Return on equity > 15 AND Debt to equity < 0.5

# Turnaround Candidate
Profit growth last year > 30 AND Profit growth 3Years > 20 AND Debt to equity < 1

# Momentum + Quality
Profit growth 5Years > 20 AND Sales growth 5Years > 20 AND Return on capital employed > 20
"""


# ─── MCP App UI Resource ──────────────────────────────────────────────────────

# The widget runs in the host's sandboxed iframe, whose CSP blocks CDN script
# loads, so the MCP Apps runtime (vendored in ui/) is inlined into the HTML.
# Its trailing ES `export {...}` is rewritten to a `globalThis.ExtApps` global
# because the inlined code runs as a classic block inside the module script.
def _inline_ext_apps(html: str) -> str:
    bundle = (_UI_DIR / "ext-apps-app-with-deps.js").read_text()

    def to_global(m: re.Match) -> str:
        pairs = []
        for part in m.group(1).split(","):
            local, _, exported = (x.strip() for x in part.partition(" as "))
            pairs.append(f"{exported or local}:{local}")
        return "globalThis.ExtApps={" + ",".join(pairs) + "};"

    bundle = re.sub(r"export\{([^}]+)\};?\s*$", to_global, bundle)
    return html.replace("/*__EXT_APPS_BUNDLE__*/", bundle)


_stock_comparison_html = _inline_ext_apps((_UI_DIR / "stock_comparison.html").read_text())


@mcp.resource(
    STOCK_COMPARISON_WIDGET_URI,
    name="Stock Comparison Dashboard",
    description="Interactive dashboard rendered for compare_stocks_ui results.",
    mime_type=MCP_APP_MIME_TYPE,
)
def stock_comparison_ui() -> str:
    """Stock Comparison Dashboard — interactive UI for comparing multiple stocks."""
    return _stock_comparison_html


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Plain HTTP health check for platforms that poll a non-MCP endpoint
    to confirm the instance is up."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    # MCP_TRANSPORT=streamable-http to run as a network server (for remote
    # MCP clients that need an HTTP server URL) instead of the default stdio
    # mode used by claude mcp add / local desktop clients.
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
