"""
Company data tools — each function is called by an MCP tool.
They fetch the Screener.in page, parse it, and return formatted text
that Claude can read naturally.
"""

import asyncio
from typing import Literal

from ..client import get_client
from ..core.company_page import CompanyPage, fetch_company_page, search_candidates
from ..core.envelope import ToolError, ToolResult
from ..core.numbers import blank_to_none, to_number
from ..core.technicals import price_freshness
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
    parse_warehouse_id,
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


async def get_financials(
    symbol: str,
    statement: Literal["profit_loss", "balance_sheet", "cash_flow", "ratios"] = "profit_loss",
    financial_type: FinancialType = "consolidated",
    years: int = 5,
) -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)

    parsers = {
        "profit_loss": (parse_profit_loss, "Profit & Loss (₹ Crore)"),
        "balance_sheet": (parse_balance_sheet, "Balance Sheet (₹ Crore)"),
        "cash_flow": (parse_cash_flow, "Cash Flow (₹ Crore)"),
        "ratios": (parse_ratios, "Key Ratios History"),
    }
    fn, title = parsers[statement]
    table = fn(page.html)
    years = max(1, min(int(years or 5), 12))

    all_years = table.get("years", [])
    shown_years = all_years[-years:]
    rows = [{"label": r.get("label", ""), "values": r.get("values", [])[-years:]} for r in table.get("rows", [])]

    data = {
        "symbol": page.symbol,
        "statement": statement,
        "title": title,
        "financial_type": page.financial_type,
        "years": shown_years,
        "rows": rows,
    }

    has_values = any(v for r in rows for v in r["values"])
    if not shown_years or not rows or not has_values:
        return page_result(
            page, data,
            missing=[statement],
            reason=f"Source page returned no {title} data for {page.symbol} [{page.financial_type}].",
        )

    warnings = []
    if statement == "ratios":
        financial = is_financial(parse_overview(page.html).get("sectors", []))
        flags = check_ratio_history(shown_years, rows, financial=financial)
        if financial:
            data["sector_note"] = "Financial company — days/working-capital sanity checks skipped (not meaningful for lenders)."
        data["rows"] = annotate_rows(shown_years, rows, flags)
        n = sum(len(f) for f in flags.values())
        if n:
            data["data_quality_flags"] = n
            warnings.append(
                f"{n} ratio value(s) fall outside plausible ranges or are internally "
                "inconsistent — marked data_quality_flag: true. Treat them as suspect "
                "(likely filing/parse artifacts), not as real business metrics."
            )
    return page_result(page, data, warnings=warnings)


def _num_or_raw(v):
    n = to_number(v)
    return n if n is not None else blank_to_none(v)


def _table_rows(rows: list[dict], label_key: str, n: int) -> list[dict]:
    return [
        {"label": r.get(label_key, ""), "values": [_num_or_raw(v) for v in r.get("values", [])[-n:]]}
        for r in rows
    ]


async def get_quarterly_results(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)
    table = parse_quarterly_results(page.html)
    quarters = table.get("years", [])[-8:]
    rows = table.get("rows", [])
    data = {
        "symbol": page.symbol,
        "financial_type": page.financial_type,
        "units": "₹ Cr (OPM % and EPS as labelled)",
        "quarters": quarters,
        "rows": _table_rows(rows, "label", len(quarters)) if quarters else [],
    }
    if not quarters or not any(v for r in rows for v in r.get("values", [])):
        return page_result(
            page, data, missing=["quarterly_results"],
            reason=f"Source page returned no quarterly results for {page.symbol} [{page.financial_type}].",
        )
    return page_result(page, data)


def _pct(v) -> float | None:
    return to_number(v)


def _shareholding(page: CompanyPage) -> tuple[list[str], list[dict]]:
    data = parse_shareholding(page.html)
    return data.get("quarters", []), data.get("rows", [])


async def get_shareholding(symbol: str) -> ToolResult:
    page = await fetch_company_page(symbol, "standalone")
    all_quarters, rows = _shareholding(page)
    quarters = all_quarters[-8:]
    data = {
        "symbol": page.symbol,
        "units": "% of shares (No. of Shareholders is a count)",
        "quarters": quarters,
        "rows": _table_rows(rows, "category", len(quarters)) if quarters else [],
    }
    if not quarters or not rows:
        return page_result(page, data, missing=["shareholding"],
                           reason=f"Source page returned no shareholding table for {page.symbol}.")

    promoter = next((r for r in data["rows"] if "promoter" in r["label"].lower()), None)
    if promoter:
        vals = [v for v in promoter["values"] if isinstance(v, (int, float))]
        if len(vals) >= 2:
            delta = round(vals[-1] - vals[0], 2)
            data["promoter_trend"] = {
                "direction": "increasing" if delta > 0.5 else "decreasing" if delta < -0.5 else "stable",
                "change_pp": delta,
                "over_quarters": len(vals),
            }
    return page_result(page, data)


async def get_promoter_pledge_history(symbol: str) -> ToolResult:
    """Dedicated view of promoter pledge % trend, pulled out of the shareholding table."""
    page = await fetch_company_page(symbol, "standalone")
    quarters, rows = _shareholding(page)
    if not quarters or not rows:
        return page_result(page, {"symbol": page.symbol, "pledge_row_found": False}, missing=["shareholding"],
                           reason=f"Source page returned no shareholding table for {page.symbol}.")

    pledge_row = next((r for r in rows if "pledge" in r.get("category", "").lower()), None)
    if not pledge_row:
        return page_result(page, {
            "symbol": page.symbol,
            "pledge_row_found": False,
            "interpretation": (
                "Screener.in shows a pledge row only when promoter shares are pledged, so its "
                "absence usually means no pledge. To be certain, check the company's SAST "
                "shareholding filings."
            ),
        })

    values = [_pct(v) for v in pledge_row.get("values", [])[-len(quarters):]]
    history = [{"quarter": q, "pledged_pct": v} for q, v in zip(quarters[-len(values):], values)]
    numeric = [v for v in values if v is not None]
    data = {"symbol": page.symbol, "pledge_row_found": True, "history": history}
    if numeric:
        latest = numeric[-1]
        data["latest_pledged_pct"] = latest
        data["severity"] = ("high" if latest > 50 else "moderate" if latest > 20
                            else "low" if latest > 0 else "none")
        data["trend"] = ("rising" if len(numeric) >= 2 and numeric[-1] > numeric[0] + 0.5
                         else "falling" if len(numeric) >= 2 and numeric[-1] < numeric[0] - 0.5
                         else "stable")
        data["severity_scale"] = ">50% high risk, 20-50% moderate, <20% low"
    return page_result(page, data)


async def get_peers(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    client = await get_client()
    page = await fetch_company_page(symbol, financial_type)

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
        return page_result(page, data, missing=["peers"],
                           reason="Screener.in's peer-comparison endpoint returned no rows.")
    return page_result(page, data)


_COMPARE_METRICS = [
    ("market_cap_cr", "market_cap"), ("current_price", "current_price"), ("pe", "pe"),
    ("book_value", "book_value"), ("roce", "roce"), ("roe", "roe"),
    ("dividend_yield", "dividend_yield"), ("52_week_high", "52_week_high"), ("52_week_low", "52_week_low"),
]


async def compare_companies(symbols: list[str], financial_type: FinancialType = "consolidated") -> ToolResult:
    """Side-by-side comparison of 2-5 companies."""
    if len(symbols) < 2:
        raise ToolError("Please provide at least 2 company symbols to compare.", "invalid_input")
    warnings = []
    if len(symbols) > 5:
        warnings.append(f"Only the first 5 of {len(symbols)} symbols were compared.")
        symbols = symbols[:5]

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
        company = {
            "symbol": page.symbol,
            "name": ov.get("name"),
            "financial_type": page.financial_type,
            "sectors": ov.get("sectors", []),
            **{out: to_number(overview_field(ov, src)) for out, src in _COMPARE_METRICS},
            "debt_to_equity": None if is_financial(ov.get("sectors", [])) else debt_to_equity(page.html),
            "other_ratios": {k: _num_or_raw(v) for k, v in ov.get("key_ratios", {}).items()
                             if k not in {"Market Cap", "Stock P/E", "Book Value", "Dividend Yield", "ROCE", "ROE"}},
        }
        if missing:
            company["missing_fields"] = missing
        companies.append(company)

    if not companies:
        raise ToolError("Could not fetch data for any of the requested companies.", "symbol_not_found", failed=failed)

    return ToolResult(
        data={"companies": companies, "failed": failed, "units": {"market_cap_cr": "₹ Cr", "prices": "₹", "ratios": "%"}},
        warnings=warnings,
        missing_fields=missing_all,
        partial=bool(failed or missing_all),
        reason="Some companies could not be fetched or had blank fields." if (failed or missing_all) else None,
    )
