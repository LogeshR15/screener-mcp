"""
Company data tools — each function is called by an MCP tool.
They fetch the Screener.in page, parse it, and return formatted text
that Claude can read naturally.
"""

import asyncio
from typing import Literal

from ..client import get_client
from ..core import history as hist
from ..core.company_page import CompanyPage, fetch_company_page, search_candidates
from ..core.envelope import ToolError, ToolResult
from ..core.numbers import blank_to_none, to_number
from ..core.technicals import price_freshness
from ..core.valuation_history import fetch_multiples, value_at_year_end
from ..core.quality import (
    OVERVIEW_CORE_FIELDS,
    annotate_rows,
    check_ratio_history,
    explain_missing,
    is_financial,
    overview_field,
    overview_missing_fields,
)
from ..parsers.company import (
    debt_to_equity,
    parse_overview,
    parse_profit_loss,
    parse_balance_sheet,
    parse_cash_flow,
    parse_quarterly_results,
    parse_ratios,
    parse_shareholding,
    parse_peers,
    parse_peers_ajax,
    parse_pros_cons,
    parse_warehouse_id,
    pledged_pct,
    split_ttm,
)


FinancialType = Literal["consolidated", "standalone"]


def page_result(page: CompanyPage, data, warnings=None, missing=None, reason=None) -> ToolResult:
    """ToolResult carrying the page's resolution/fallback notes and meta."""
    return ToolResult(
        data=data,
        warnings=page.warnings + list(warnings or []),
        missing_fields=list(missing or []),
        reason=reason,
        meta=page.meta,
    )


def overview_data(page: CompanyPage, ov: dict) -> tuple[dict, list[str], str | None]:
    """Clean overview dict (numbers, None for blanks) + missing fields + reason."""
    missing = overview_missing_fields(ov)
    reason = explain_missing(missing, len(missing) == len(OVERVIEW_CORE_FIELDS)) if missing else None

    price = to_number(overview_field(ov, "current_price"))
    high = to_number(overview_field(ov, "52_week_high"))
    low = to_number(overview_field(ov, "52_week_low"))
    data = {
        "symbol": page.symbol,
        "name": ov.get("name"),
        "nse_code": blank_to_none(ov.get("nse_code")),
        "bse_code": blank_to_none(ov.get("bse_code")),
        "sectors": ov.get("sectors", []),
        "financial_type": page.financial_type,
        "current_price": price,
        "52_week_high": high,
        "52_week_low": low,
        "pct_above_52w_low": round((price / low - 1) * 100, 2) if price and low else None,
        "pct_below_52w_high": round((1 - price / high) * 100, 2) if price and high else None,
        "key_ratios": {
            k: (to_number(v) if to_number(v) is not None else blank_to_none(v))
            for k, v in ov.get("key_ratios", {}).items()
        },
        "units": {"market_cap": "₹ Cr", "prices": "₹", "ratios": "% where applicable"},
    }
    return data, missing, reason


async def search_company(query: str) -> ToolResult:
    """Search for a company by name or NSE/BSE symbol."""
    candidates = await search_candidates(query)
    results = [
        {
            "symbol": c.symbol,
            "name": c.name,
            "screener_id": c.company_id,
            "has_consolidated_financials": c.has_consolidated,
        }
        for c in candidates[:10]
    ]
    warnings = []
    if not results:
        warnings.append(
            f"No companies found matching '{query}'. Try the full company name or stock symbol."
        )
    return ToolResult(data={"query": query, "results": results}, warnings=warnings)


async def get_company_overview(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    """Get a comprehensive overview of a company."""
    page = await fetch_company_page(symbol, financial_type)
    ov = parse_overview(page.html)
    data, missing, reason = overview_data(page, ov)
    data["about"] = blank_to_none(ov.get("about"))
    data["screener_analysis"] = parse_pros_cons(page.html)
    warnings = []
    if page.company_id:
        # A bare "current price" can't be judged without a timestamp.
        try:
            freshness = await price_freshness(page.company_id)
        except Exception as e:
            freshness = {}
            warnings.append(f"Couldn't determine how fresh the price is ({type(e).__name__}).")
        if freshness:
            data["price_freshness"] = freshness
            if freshness.get("stale"):
                warnings.append(freshness["stale_note"])
    return page_result(page, data, warnings=warnings, missing=missing, reason=reason)


STATEMENTS = {
    "profit_loss": (parse_profit_loss, "Profit & Loss (₹ Crore)"),
    "balance_sheet": (parse_balance_sheet, "Balance Sheet (₹ Crore)"),
    "cash_flow": (parse_cash_flow, "Cash Flow (₹ Crore)"),
    "ratios": (parse_ratios, "Key Ratios History"),
}
MAX_YEARS = 12

_RATIO_SOURCES = {
    "screener": "Screener.in ratios table",
    "ROE %": "computed: net profit ÷ average shareholders' equity (equity capital + reserves)",
    "Debt to equity": "computed: borrowings ÷ (equity capital + reserves), year-end",
    "P/E (year-end)": "Screener.in P/E chart, last trading day of the fiscal year",
    "P/B (year-end)": "Screener.in P/B chart, last trading day of the fiscal year",
}


def _num_or_raw(v):
    n = to_number(v)
    return n if n is not None else blank_to_none(v)


def _table_rows(rows: list[dict], label_key: str, n: int) -> list[dict]:
    return [
        {"label": r.get(label_key, ""), "values": [_num_or_raw(v) for v in r.get("values", [])[-n:]]}
        for r in rows
    ]


async def _computed_ratio_rows(page: CompanyPage, years: list[str], financial: bool,
                               warnings: list[str], missing: list[str]) -> list[dict]:
    """ROE, debt-to-equity and year-end P/E, P/B — the history Screener's
    ratios table doesn't carry, aligned to its fiscal-year columns."""
    pl, bs = parse_profit_loss(page.html), parse_balance_sheet(page.html)
    rows = [{"label": "ROE %", "values": [hist.roe_by_year(pl, bs).get(y) for y in years]}]
    if not financial:
        de = hist.debt_to_equity_by_year(bs)
        rows.append({"label": "Debt to equity", "values": [de.get(y) for y in years]})
    if page.company_id:
        try:
            multiples = await fetch_multiples(page.company_id, page.financial_type == "consolidated")
            rows.append({"label": "P/E (year-end)", "values": [value_at_year_end(multiples["pe"], y) for y in years]})
            rows.append({"label": "P/B (year-end)", "values": [value_at_year_end(multiples["pb"], y) for y in years]})
        except Exception as e:
            missing.append("valuation_history")
            warnings.append(f"Year-end P/E and P/B unavailable — Screener's chart API failed ({type(e).__name__}).")
    return [r for r in rows if any(v is not None for v in r["values"])]


async def statement_data(page: CompanyPage, statement: str, years: int = 5) -> tuple[dict, list[str], list[str], str | None]:
    """One statement as structured data → (data, warnings, missing, reason).

    `years` counts fiscal years; the P&L's TTM column is returned separately
    as `ttm`, never as one of the years.
    """
    fn, title = STATEMENTS[statement]
    years = max(1, min(int(years or 5), MAX_YEARS))
    fiscal, rows, ttm = split_ttm(fn(page.html))
    shown = fiscal[-years:]
    rows = [{"label": r["label"], "values": r["values"][-len(shown):] if shown else []} for r in rows]

    data = {
        "symbol": page.symbol,
        "statement": statement,
        "title": title,
        "financial_type": page.financial_type,
        "units": "₹ Cr unless the label says % or days",
        "years": shown,
        "rows": rows,
    }
    if ttm:
        data["ttm"] = {label: _num_or_raw(v) for label, v in ttm.items()}
    if len(fiscal) < years and shown:
        data["years_note"] = f"Only {len(fiscal)} fiscal year(s) are on the source page."

    warnings: list[str] = []
    missing: list[str] = []
    if not shown or not any(v for r in rows for v in r["values"]):
        return data, warnings, [statement], f"Source page returned no {title} data for {page.symbol} [{page.financial_type}]."

    if statement == "ratios":
        financial = is_financial(parse_overview(page.html).get("sectors", []))
        flags = check_ratio_history(shown, rows, financial=financial)
        if financial:
            data["sector_note"] = ("Financial company — days/working-capital sanity checks and debt-to-equity "
                                   "skipped (leverage is the business model for lenders).")
        n = sum(len(f) for f in flags.values())
        if n:
            data["data_quality_flags"] = n
            warnings.append(
                f"{n} ratio value(s) fall outside plausible ranges or are internally "
                "inconsistent — marked data_quality_flag: true. Treat them as suspect "
                "(likely filing/parse artifacts), not as real business metrics."
            )
        rows = [{**r, "values": [_num_or_raw(v) for v in r["values"]]} for r in annotate_rows(shown, rows, flags)]
        computed = await _computed_ratio_rows(page, shown, financial, warnings, missing)
        data["rows"] = rows + computed
        data["row_sources"] = {r["label"]: _RATIO_SOURCES["screener"] for r in rows}
        data["row_sources"].update({r["label"]: _RATIO_SOURCES[r["label"]] for r in computed})
    else:
        data["rows"] = [{**r, "values": [_num_or_raw(v) for v in r["values"]]} for r in rows]
    return data, warnings, missing, None


async def get_financials(
    symbol: str,
    statement: Literal["profit_loss", "balance_sheet", "cash_flow", "ratios"] = "profit_loss",
    financial_type: FinancialType = "consolidated",
    years: int = 5,
) -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)
    data, warnings, missing, reason = await statement_data(page, statement, years)
    return page_result(page, data, warnings=warnings, missing=missing, reason=reason)


QUARTERS = 8


def quarterly_data(page: CompanyPage) -> tuple[dict, list[str], str | None]:
    """Last 8 quarters → (data, missing, reason)."""
    table = parse_quarterly_results(page.html)
    quarters = table.get("years", [])[-QUARTERS:]
    rows = table.get("rows", [])
    data = {
        "symbol": page.symbol,
        "financial_type": page.financial_type,
        "units": "₹ Cr (OPM % and EPS as labelled)",
        "quarters": quarters,
        "rows": _table_rows(rows, "label", len(quarters)) if quarters else [],
    }
    if not quarters or not any(v for r in rows for v in r.get("values", [])):
        return data, ["quarterly_results"], (
            f"Source page returned no quarterly results for {page.symbol} [{page.financial_type}].")
    return data, [], None


async def get_quarterly_results(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)
    data, missing, reason = quarterly_data(page)
    return page_result(page, data, missing=missing, reason=reason)


SHAREHOLDING_QUARTERS = 8


def _trend(values: list) -> dict | None:
    """First→last and last-quarter change of a % series, in percentage points."""
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return None
    change = round(vals[-1] - vals[0], 2)
    return {
        "latest_pct": vals[-1],
        "change_pp": change,
        "last_quarter_change_pp": round(vals[-1] - vals[-2], 2),
        "over_quarters": len(vals),
        "direction": "increasing" if change > 0.5 else "decreasing" if change < -0.5 else "stable",
    }


def _pledge(html: str, has_promoter: bool) -> dict:
    if not has_promoter:
        return {
            "status": "not_applicable",
            "note": "No promoter group, so there are no promoter shares to pledge.",
        }
    pct = pledged_pct(html)
    if pct is None:
        return {
            "status": "not_flagged",
            "note": ("Screener's analysis doesn't flag a promoter pledge for this company. Small pledges may "
                     "not be flagged — confirm in the company's SAST / Reg 31 pledge disclosures before relying "
                     "on it."),
        }
    return {
        "status": "reported",
        "pledged_pct_of_promoter_holding": pct,
        "severity": "high" if pct > 50 else "moderate" if pct > 20 else "low" if pct > 0 else "none",
        "severity_scale": ">50% high risk, 20-50% moderate, >0-20% low",
        "source": "Screener.in analysis (latest disclosure; no quarterly history on the page)",
    }


def shareholding_data(page: CompanyPage, n: int = SHAREHOLDING_QUARTERS) -> tuple[dict, list[str], str | None]:
    """Shareholding table + per-category trends + promoter / pledge assessment
    → (data, missing, reason)."""
    table = parse_shareholding(page.html)
    quarters = table.get("quarters", [])[-n:]
    rows = _table_rows(table.get("rows", []), "category", len(quarters)) if quarters else []
    data: dict = {
        "symbol": page.symbol,
        "units": "% of shares (No. of Shareholders is a count)",
        "quarters": quarters,
        "rows": rows,
    }
    if not quarters or not rows:
        return data, ["shareholding"], f"Source page returned no shareholding table for {page.symbol}."

    data["trends"] = {r["label"]: t for r in rows
                      if "shareholders" not in r["label"].lower() and (t := _trend(r["values"]))}
    promoter = next((r for r in rows if r["label"].lower().startswith("promoter")), None)
    if promoter:
        data["promoter_group"] = {"present": True, "trend": _trend(promoter["values"])}
    else:
        data["promoter_group"] = {
            "present": False,
            "note": ("No promoter row in the shareholding pattern — a widely held, professionally "
                     "managed company (e.g. ITC). Promoter-holding and pledge checks don't apply; "
                     "watch the institutional (FII/DII) trend instead."),
        }
    data["pledge"] = _pledge(page.html, bool(promoter))
    return data, [], None


async def get_shareholding(symbol: str) -> ToolResult:
    page = await fetch_company_page(symbol, "standalone")
    data, missing, reason = shareholding_data(page)
    return page_result(page, data, missing=missing, reason=reason)


async def peers_data(page: CompanyPage) -> tuple[dict, list[str], str | None]:
    """Peer table → (data, missing, reason)."""
    client = await get_client()
    # Screener.in's real peer table is loaded client-side via an AJAX call
    # keyed on the company's "warehouse id" (distinct from its numeric id):
    #   GET /api/company/{warehouse_id}/peers/
    warehouse_id = parse_warehouse_id(page.html)
    peers: list[dict[str, str]] = []
    if warehouse_id:
        peers = parse_peers_ajax(await client.get_html(f"/api/company/{warehouse_id}/peers/"))
    if not peers:
        peers = parse_peers(page.html)

    sectors = [p for p in peers if "Sector Level" in p]
    rows = [p for p in peers if not p.get("_note") and "Sector Level" not in p]
    columns = [c for c in (rows[0].keys() if rows else []) if not c.startswith("_") and c != "S.No."]
    data = {
        "symbol": page.symbol,
        "financial_type": page.financial_type,
        "columns": columns,
        "rows": [{c: _num_or_raw(r.get(c)) if c != "Name" else r.get(c) for c in columns} for r in rows],
    }
    if sectors:
        data["sector_context"] = {s["Sector Level"]: s["Name"] for s in sectors}
    if not rows:
        return data, ["peers"], "Screener.in's peer-comparison endpoint returned no rows."
    return data, [], None


async def get_peers(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)
    data, missing, reason = await peers_data(page)
    return page_result(page, data, missing=missing, reason=reason)


_COMPARE_METRICS = [
    ("market_cap_cr", "market_cap"), ("current_price", "current_price"), ("pe", "pe"),
    ("book_value", "book_value"), ("roce", "roce"), ("roe", "roe"),
    ("dividend_yield", "dividend_yield"), ("52_week_high", "52_week_high"), ("52_week_low", "52_week_low"),
]


MAX_COMPARE = 6


def snapshot_flags(c: dict, financial: bool) -> list[dict[str, str]]:
    """Point-in-time threshold checks on one company's comparison row. For
    checks over the full history use analyze_red_flags."""
    flags = []
    de = c.get("debt_to_equity")
    if not financial and de is not None:
        if de > 1.5:
            flags.append({"flag": f"High debt-to-equity ratio ({de:.2f})", "severity": "critical"})
        elif de > 0.8:
            flags.append({"flag": f"Elevated debt-to-equity ratio ({de:.2f})", "severity": "warning"})
    if not financial and c.get("roce") is not None and c["roce"] < 10:
        flags.append({"flag": f"Low return on capital employed ({c['roce']:.1f}%)", "severity": "warning"})
    if c.get("roe") is not None and c["roe"] < 10:
        flags.append({"flag": f"Low return on equity ({c['roe']:.1f}%)", "severity": "warning"})
    if c.get("pe") is not None and c["pe"] > 60:
        flags.append({"flag": f"Very high valuation — P/E of {c['pe']:.1f}", "severity": "info"})
    return flags


def _parse_symbols(symbols: list[str] | str) -> list[str]:
    if isinstance(symbols, str):
        symbols = symbols.split(",")
    return [s.strip() for s in symbols if s and s.strip()]


async def compare_companies(symbols: list[str] | str, financial_type: FinancialType = "consolidated") -> ToolResult:
    """Side-by-side comparison of 2-6 companies."""
    symbols = _parse_symbols(symbols)
    if len(symbols) < 2:
        raise ToolError("Please provide at least 2 company symbols to compare.", "invalid_input")
    warnings = []
    if len(symbols) > MAX_COMPARE:
        warnings.append(f"Only the first {MAX_COMPARE} of {len(symbols)} symbols were compared.")
        symbols = symbols[:MAX_COMPARE]

    async def fetch(sym: str):
        page = await fetch_company_page(sym, financial_type)
        return page, parse_overview(page.html)

    results = await asyncio.gather(*[fetch(s) for s in symbols], return_exceptions=True)

    companies, failed, missing_all = [], [], []
    for requested, r in zip(symbols, results):
        if isinstance(r, Exception):
            entry = {"requested_symbol": requested, "error": str(r)}
            if isinstance(r, ToolError) and r.details.get("candidates"):
                entry["candidates"] = r.details["candidates"]
            failed.append(entry)
            hint = f" Candidates: {', '.join(c['symbol'] for c in entry['candidates'])}." if entry.get("candidates") else ""
            warnings.append(f"Could not fetch {requested}: {r}{hint}")
            continue
        page, ov = r
        warnings += page.warnings
        missing = overview_missing_fields(ov)
        missing_all += [f"{page.symbol}.{m}" for m in missing]
        financial = is_financial(ov.get("sectors", []))
        company = {
            "symbol": page.symbol,
            "name": ov.get("name"),
            "financial_type": page.financial_type,
            "sectors": ov.get("sectors", []),
            **{out: to_number(overview_field(ov, src)) for out, src in _COMPARE_METRICS},
            "debt_to_equity": None if financial else debt_to_equity(page.html),
            "other_ratios": {k: _num_or_raw(v) for k, v in ov.get("key_ratios", {}).items()
                             if k not in {"Market Cap", "Stock P/E", "Book Value", "Dividend Yield", "ROCE", "ROE"}},
        }
        if financial:
            company["debt_to_equity_note"] = "Not meaningful for a lender — leverage is the business model."
        company["red_flags"] = snapshot_flags(company, financial)
        if missing:
            company["missing_fields"] = missing
        companies.append(company)

    if not companies:
        raise ToolError("Could not fetch data for any of the requested companies.", "symbol_not_found", failed=failed)

    return ToolResult(
        data={"companies": companies, "failed": failed,
              "units": {"market_cap_cr": "₹ Cr", "prices": "₹", "ratios": "%"},
              "red_flags_note": "red_flags are point-in-time threshold checks; analyze_red_flags checks the full history."},
        warnings=warnings,
        missing_fields=missing_all,
        partial=bool(failed or missing_all),
        reason="Some companies could not be fetched or had blank fields." if (failed or missing_all) else None,
    )


def dashboard_rows(data: dict) -> list[dict]:
    """compare_companies data in the shape the comparison dashboard renders."""
    rows = [{
        "symbol": c["symbol"], "name": c.get("name"), "financial_type": c.get("financial_type"),
        "price": c.get("current_price"), "market_cap": c.get("market_cap_cr"), "pe_ratio": c.get("pe"),
        "roe": c.get("roe"), "roce": c.get("roce"), "debt_to_equity": c.get("debt_to_equity"),
        "dividend_yield": c.get("dividend_yield"), "book_value": c.get("book_value"),
        "missing_fields": c.get("missing_fields", []), "red_flags": c.get("red_flags", []),
    } for c in data.get("companies", [])]
    for f in data.get("failed", []):
        entry = {"symbol": str(f.get("requested_symbol", "")).upper(), "error": f.get("error")}
        if f.get("candidates"):
            entry["candidates"] = f["candidates"]
        rows.append(entry)
    return rows
