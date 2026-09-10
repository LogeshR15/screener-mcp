"""
Screener.in MCP Server — Indian Stock Research Assistant

Tools exposed to Claude (25 total):
  search_company              — find a company by name or symbol
  get_company_overview        — key ratios, about, price data
  get_financials              — P&L / Balance Sheet / Cash Flow / Ratios history
  get_quarterly_results       — last 8 quarters of results
  get_shareholding_pattern    — promoter / FII / DII / public holding trend
  get_peer_comparison         — peer comparison table
  compare_companies           — side-by-side comparison of 2-5 companies
  screen_stocks               — custom Screener.in query
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
  get_promoter_pledge_history — dedicated promoter pledge % trend with severity flag
  get_commodity_prices        — commodity price context and company impact analysis
  notebook_ai                 — save and summarize investment research notes

Resources:
  screener://analyst-guide    — how to use this assistant
  screener://query-syntax     — Screener query language reference
  screener://stock-comparison — interactive stock comparison dashboard UI

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
import json
import os
import re
from pathlib import Path
from mcp.server.fastmcp import FastMCP
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
from .tools.announcements import get_company_announcements as _get_announcements
from .tools.shareholders import (
    search_shareholder as _search_shareholder,
    get_bulk_deals as _get_bulk_deals,
)
from .tools.commodities import get_commodity_prices as _get_commodity_prices
from .tools.notebook import notebook_ai as _notebook_ai

def _safe(result):
    """Wrap a coroutine so network/auth errors become readable messages."""
    import asyncio, functools
    async def wrapper(*args, **kwargs):
        try:
            return await result(*args, **kwargs)
        except PermissionError as e:
            return f"**Login required.** {e}\n\nSet `SCREENER_USERNAME` and `SCREENER_PASSWORD` env vars, then restart the server."
        except httpx.TimeoutException:
            return "**Request timed out.** Screener.in is taking too long to respond — try again in a moment."
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"**Company not found.** Check the symbol and try `search_company()` to find the correct NSE/BSE code."
            return f"**Screener.in returned an error** ({e.response.status_code}). The site may be down — try again shortly."
        except httpx.NetworkError:
            return "**Cannot reach Screener.in.** Check your internet connection and try again."
        except Exception as e:
            return f"**Unexpected error**: {type(e).__name__}: {e}\n\nIf this persists, please open an issue at https://github.com/LogeshR15/screener-mcp/issues"
    functools.update_wrapper(wrapper, result)
    return wrapper


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

@mcp.tool()
async def search_company(query: str) -> str:
    """
    Search for an Indian stock/company by name or NSE/BSE symbol.

    Examples:
      search_company("Tata Consultancy")
      search_company("INFY")
      search_company("Jyothy Labs")
      search_company("ITC")
    """
    return await _safe(_search_company)(query)


@mcp.tool()
async def screen_stocks(query: str, limit: int = 25) -> str:
    """
    Run a custom stock screen using Screener.in query syntax.

    Query field names (exact spelling matters):
      Market Capitalization, Current Price, Price to Earning, Price to book value,
      Return on capital employed, Return on equity, Debt to equity,
      Sales growth 5Years, Sales growth 3Years, Sales growth last year,
      Profit growth 5Years, Profit growth 3Years, Profit growth last year,
      Dividend yield, Pledged percentage, Net cash flow last year,
      Average return on equity 5Years, Average return on capital employed 5Years,
      Current ratio, EV / EBITDA, PEG Ratio

    Operators: > < = AND

    Examples:
      "Market Capitalization < 5000 AND Return on capital employed > 15 AND Debt to equity < 0.5"
      "Profit growth 5Years > 20 AND Sales growth 5Years > 15 AND Debt to equity < 0.3"
      "Dividend yield > 3 AND Debt to equity < 0.5 AND Return on equity > 15"
    """
    return await _safe(_screen)(query, limit=limit)


@mcp.tool()
async def screen_by_theme(theme: str, limit: int = 20) -> str:
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


@mcp.tool()
async def list_investment_themes() -> str:
    """
    List all available pre-built investment themes with their screening criteria.
    Use this to discover what thematic screens are available.
    """
    return await _safe(_list_themes)()


# ─── Company Deep Dive ─────────────────────────────────────────────────────────

@mcp.tool()
async def get_company_overview(symbol: str, financial_type: str = "consolidated") -> str:
    """
    Get a company's key ratios, current price, 52-week range, and about section.

    symbol: NSE/BSE symbol (e.g., "TCS", "INFY", "RELIANCE", "HDFCBANK")
    financial_type: "consolidated" (default) or "standalone"

    This is the best starting point for any company analysis.
    """
    return await _safe(_get_overview)(symbol, financial_type)


@mcp.tool()
async def get_financials(
    symbol: str,
    statement: str = "profit_loss",
    financial_type: str = "consolidated",
    years: int = 5,
) -> str:
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
        return f"Invalid statement type '{statement}'. Choose from: {', '.join(valid)}"
    return await _safe(_get_financials)(symbol, statement, financial_type, years)


@mcp.tool()
async def get_quarterly_results(symbol: str, financial_type: str = "consolidated") -> str:
    """
    Get the last 8 quarters of results for a company.

    Shows: Revenue, Expenses, Operating Profit, OPM%, Net Profit, EPS
    Useful for identifying quarter-on-quarter growth trends and seasonality.

    symbol: NSE/BSE symbol (e.g., "TCS", "WIPRO", "MARUTI")
    """
    return await _safe(_get_quarterly)(symbol, financial_type)


@mcp.tool()
async def get_shareholding_pattern(symbol: str) -> str:
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


@mcp.tool()
async def get_peer_comparison(symbol: str, financial_type: str = "consolidated") -> str:
    """
    Get peer comparison table as shown on Screener.in for a company.

    Shows the company alongside its sector peers with key metrics
    like Market Cap, Sales, Net Profit, PE, ROCE, etc.

    symbol: NSE/BSE symbol
    """
    return await _safe(_get_peers)(symbol, financial_type)


@mcp.tool()
async def compare_companies(symbols: list[str], financial_type: str = "consolidated") -> str:
    """
    Side-by-side comparison of 2 to 5 companies on all key ratios.

    Fetches data for each company and presents them in a comparative table.
    Best for "ITC vs HUL vs Nestle" type questions.

    symbols: list of NSE/BSE symbols, e.g., ["ITC", "HUL", "NESTLE"]
    financial_type: "consolidated" or "standalone"

    Examples:
      compare_companies(["ITC", "HUL"])
      compare_companies(["TCS", "INFY", "WIPRO", "HCLTECH"])
      compare_companies(["PIDILITIND", "ASIANPAINT", "BERGEPAINT"])
    """
    return await _safe(_compare)(symbols, financial_type)


# ─── Analysis Tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def get_full_analysis(symbol: str, financial_type: str = "consolidated") -> str:
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


@mcp.tool()
async def analyze_red_flags(symbol: str, financial_type: str = "consolidated") -> str:
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


@mcp.tool()
async def explain_for_beginners(symbol: str) -> str:
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


def _compute_red_flags(ratios: dict[str, str]) -> list[dict[str, str]]:
    """Deterministic, rule-based red flag checks computed from a single snapshot of ratios.

    Not a substitute for the LLM-driven analyze_red_flags tool (which reasons over
    full history) — this only checks point-in-time thresholds so the dashboard has
    something real to render without an extra model round-trip.
    """
    flags: list[dict[str, str]] = []

    debt_equity = _num(_find_ratio(ratios, "Debt to equity"))
    if debt_equity is not None:
        if debt_equity > 1.5:
            flags.append({"flag": f"High debt-to-equity ratio ({debt_equity:.2f})", "severity": "critical"})
        elif debt_equity > 0.8:
            flags.append({"flag": f"Elevated debt-to-equity ratio ({debt_equity:.2f})", "severity": "warning"})

    roce = _num(_find_ratio(ratios, "Return on capital employed", "ROCE"))
    if roce is not None and roce < 10:
        flags.append({"flag": f"Low return on capital employed ({roce:.1f}%)", "severity": "warning"})

    roe = _num(_find_ratio(ratios, "Return on equity", "ROE"))
    if roe is not None and roe < 10:
        flags.append({"flag": f"Low return on equity ({roe:.1f}%)", "severity": "warning"})

    pe = _num(_find_ratio(ratios, "Stock P/E", "P/E"))
    if pe is not None and pe > 60:
        flags.append({"flag": f"Very high valuation — P/E of {pe:.1f}", "severity": "info"})

    return flags


@mcp.tool()
async def compare_stocks_ui(symbols: list[str] | str) -> dict:
    """
    Interactive stock comparison dashboard.

    Compare multiple stocks side-by-side with real-time price, market cap,
    P/E, ROE, ROCE, debt levels, and rule-based red flag checks.
    Renders as an interactive dashboard in Claude Desktop.

    Args:
        symbols: Stock symbols, either as a list (["TCS", "INFY", "WIPRO"])
            or a comma-separated string ("TCS,INFY,WIPRO")

    Examples:
      compare_stocks_ui(["TCS", "INFY", "WIPRO"])
      compare_stocks_ui(["HDFCBANK", "ICICIBANK", "AXISBANK"])
      compare_stocks_ui("HUL,ITC,NESTLEIND")

    Note: use NSE trading symbols, not company names (e.g. "INFY" not "INFOSYS").
    """
    import asyncio
    from .client import get_client
    from .parsers.company import parse_overview

    if isinstance(symbols, str):
        symbols = symbols.split(",")
    stock_list = [s.strip().upper() for s in symbols if s.strip()][:6]
    client = await get_client()

    async def fetch(sym: str):
        html = await client.get_html(f"/company/{sym}/consolidated/")
        return parse_overview(html)

    results = await asyncio.gather(*[fetch(s) for s in stock_list], return_exceptions=True)

    stocks = []
    for sym, result in zip(stock_list, results):
        if isinstance(result, Exception):
            stocks.append({"symbol": sym, "error": "Symbol not found — use the NSE trading symbol (e.g. INFY, not INFOSYS)."})
            continue

        ratios = result.get("key_ratios", {})
        stocks.append({
            "symbol": sym,
            "name": result.get("name"),
            "price": result.get("current_price"),
            "market_cap": _find_ratio(ratios, "Market Cap"),
            "pe_ratio": _find_ratio(ratios, "Stock P/E", "P/E"),
            "roe": _find_ratio(ratios, "Return on equity", "ROE"),
            "roce": _find_ratio(ratios, "Return on capital employed", "ROCE"),
            "debt_to_equity": _find_ratio(ratios, "Debt to equity"),
            "dividend_yield": _find_ratio(ratios, "Dividend Yield"),
            "book_value": _find_ratio(ratios, "Book Value"),
            "red_flags": _compute_red_flags(ratios),
        })

    return {
        "stocks": stocks,
        "count": len([s for s in stocks if "error" not in s]),
    }


# ─── Document Analysis ────────────────────────────────────────────────────────

@mcp.tool()
async def get_document_list(symbol: str) -> str:
    """
    List all available annual reports and earnings call transcripts for a company.

    Fetches from Screener.in company page and NSE API.

    symbol: NSE/BSE symbol (e.g., "TCS", "INFY")

    Use this before calling analyze_annual_report or analyze_earnings_call
    to see what documents are available and their years/quarters.
    """
    return await _safe(_get_document_list)(symbol)


@mcp.tool()
async def analyze_annual_report(
    symbol: str,
    year: int,
    question: str = "",
    pdf_url: str = "",
) -> str:
    """
    Ask any question about a company's annual report using AI-powered semantic search.

    Downloads the PDF, indexes it into a local vector database (ChromaDB),
    and retrieves the most relevant sections to answer your question.
    Results are cached — subsequent calls on the same report are instant.

    symbol: NSE/BSE symbol (e.g., "TCS")
    year: report year (e.g., 2024, 2023)
    question: what you want to know (leave blank for a general summary)
    pdf_url: optional — provide directly if you have the link

    Requires: pip install pdfplumber sentence-transformers chromadb

    Examples:
      analyze_annual_report("TCS", 2024, "What are the key risks mentioned?")
      analyze_annual_report("INFY", 2023, "What did management say about margins?")
      analyze_annual_report("RELIANCE", 2024, "Summarize the new energy segment")
    """
    return await _safe(_analyze_annual_report)(symbol, year, question, pdf_url or None)


@mcp.tool()
async def analyze_earnings_call(
    symbol: str,
    quarter: str,
    question: str = "",
    pdf_url: str = "",
) -> str:
    """
    Ask any question about an earnings call transcript using semantic search.

    Same RAG pipeline as analyze_annual_report — downloads, indexes, and retrieves
    relevant sections from the transcript PDF.

    symbol: NSE/BSE symbol
    quarter: e.g., "Q1FY25", "Q2FY26", "Q3FY25"
    question: what you want to know (leave blank for a management commentary summary)
    pdf_url: optional — provide directly if you have the link

    Requires: pip install pdfplumber sentence-transformers chromadb

    Examples:
      analyze_earnings_call("HDFCBANK", "Q3FY25", "What is the guidance on NIM?")
      analyze_earnings_call("TCS", "Q2FY25", "What did they say about deal wins?")
    """
    return await _safe(_analyze_earnings_call)(symbol, quarter, question, pdf_url or None)


@mcp.tool()
async def ask_company_research(
    symbol: str,
    question: str,
    max_annual_reports: int = 3,
    max_earnings_calls: int = 4,
) -> str:
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


@mcp.tool()
async def search_market_commentary(
    question: str,
    symbols: list[str],
    top_k_per_symbol: int = 3,
) -> str:
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

@mcp.tool()
async def get_company_announcements(
    symbol: str,
    category: str = "all",
    days: int = 30,
) -> str:
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


@mcp.tool()
async def search_shareholder(
    name: str,
    symbol: str = "",
    days: int = 365,
) -> str:
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


@mcp.tool()
async def get_bulk_deals(symbol: str, days: int = 90) -> str:
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


@mcp.tool()
async def get_promoter_pledge_history(symbol: str) -> str:
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


# ─── Commodity Analysis ────────────────────────────────────────────────────────

@mcp.tool()
async def get_commodity_prices(commodity: str, years: int = 5) -> str:
    """
    Get commodity price context and its impact on Indian listed companies.

    Covers which companies benefit or suffer from price moves, and suggested
    Screener queries to find companies exposed to this commodity.

    commodity: gold | silver | crude_oil | copper | aluminium | zinc | nickel
               | cotton | natural_gas | steel
    years: historical context period (1–10, default 5)

    Examples:
      get_commodity_prices("crude_oil")
      get_commodity_prices("copper", years=3)
      get_commodity_prices("gold")
    """
    return await _safe(_get_commodity_prices)(commodity, years)


# ─── Research Notebook ────────────────────────────────────────────────────────

@mcp.tool()
async def notebook_ai(
    action: str,
    symbol: str = "",
    content: str = "",
    note_id: str = "",
) -> str:
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

# Load the stock comparison dashboard UI bundle
_ui_bundle_path = Path(__file__).parent.parent.parent / "apps" / "stock-comparison-dashboard" / "dist" / "index.html"
_ui_bundle = ""
if _ui_bundle_path.exists():
    _ui_bundle = _ui_bundle_path.read_text()
else:
    _ui_bundle = """
    <html>
        <head><title>Stock Comparison Dashboard</title></head>
        <body style="padding: 20px; font-family: system-ui; color: #666;">
            <p>⚠️ <strong>Stock comparison UI not found.</strong></p>
            <p>Build it with: <code>cd apps/stock-comparison-dashboard && npm run build</code></p>
        </body>
    </html>
    """


@mcp.resource("screener://stock-comparison")
def stock_comparison_ui() -> str:
    """Stock Comparison Dashboard — interactive UI for comparing multiple stocks."""
    return _ui_bundle


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
