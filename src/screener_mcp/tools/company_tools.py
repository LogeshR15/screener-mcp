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
from ..core.quality import (
    OVERVIEW_CORE_FIELDS,
    annotate_rows,
    check_ratio_history,
    explain_missing,
    overview_field,
    overview_missing_fields,
)
from ..parsers.company import (
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
    return page_result(page, data, missing=missing, reason=reason)


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
        flags = check_ratio_history(shown_years, rows)
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


async def get_quarterly_results(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)
    symbol, financial_type = page.symbol, page.financial_type
    data = parse_quarterly_results(page.html)

    years = data.get("years", [])[-8:]
    rows = data.get("rows", [])
    if not years or not rows or not any(v for r in rows for v in r.get("values", [])):
        return page_result(
            page, {"report": f"No quarterly results available for {symbol}."},
            missing=["quarterly_results"],
            reason=f"Source page returned no quarterly results for {symbol} [{financial_type}].",
        )

    lines = [
        f"## {symbol} — Quarterly Results (₹ Crore) [{financial_type}]",
        "",
        f"{'Metric':<30} " + "  ".join(f"{y:>12}" for y in years),
        "-" * (30 + 15 * len(years)),
    ]
    for row in rows:
        label = row.get("label", "")
        all_vals = row.get("values", [])
        values = all_vals[-len(years):]
        # right-pad if fewer values than years
        values = values + [""] * (len(years) - len(values))
        lines.append(f"{label:<30} " + "  ".join(f"{v:>12}" for v in values))

    return page_result(page, {"report": "\n".join(lines)})


async def get_shareholding(symbol: str) -> ToolResult:
    page = await fetch_company_page(symbol, "standalone")
    symbol = page.symbol
    data = parse_shareholding(page.html)

    quarters = data.get("quarters", [])[-8:]
    rows = data.get("rows", [])
    if not quarters or not rows:
        return page_result(
            page, {"report": f"No shareholding data found for {symbol}."},
            missing=["shareholding"],
            reason=f"Source page returned no shareholding table for {symbol}.",
        )

    lines = [
        f"## {symbol} — Shareholding Pattern (%)",
        "",
        f"{'Category':<25} " + "  ".join(f"{q:>10}" for q in quarters),
        "-" * (25 + 13 * len(quarters)),
    ]
    for row in rows:
        cat = row.get("category", "")
        all_vals = row.get("values", [])
        values = all_vals[-len(quarters):]
        values = values + [""] * (len(quarters) - len(values))
        lines.append(f"{cat:<25} " + "  ".join(f"{v:>10}" for v in values))

    # Promoter trend analysis
    promoter_row = next((r for r in rows if "promoter" in r.get("category", "").lower()), None)
    if promoter_row and len(promoter_row.get("values", [])) >= 2:
        vals = promoter_row["values"]
        try:
            latest = float(vals[-1].replace("%", "").strip())
            oldest = float(vals[0].replace("%", "").strip())
            delta = latest - oldest
            trend = "increasing" if delta > 0.5 else "decreasing" if delta < -0.5 else "stable"
            sign = "+" if delta >= 0 else ""
            lines += [
                "",
                f"**Promoter holding trend**: {trend} ({sign}{delta:.1f}% over shown period)",
            ]
        except (ValueError, IndexError):
            pass

    return page_result(page, {"report": "\n".join(lines)})


async def get_promoter_pledge_history(symbol: str) -> ToolResult:
    """Dedicated view of promoter pledge % trend, pulled out of the shareholding table."""
    page = await fetch_company_page(symbol, "standalone")
    symbol = page.symbol
    data = parse_shareholding(page.html)

    quarters = data.get("quarters", [])
    rows = data.get("rows", [])
    if not quarters or not rows:
        return page_result(
            page, {"report": f"No shareholding data found for {symbol}."},
            missing=["shareholding"],
            reason=f"Source page returned no shareholding table for {symbol}.",
        )

    pledge_row = next(
        (r for r in rows if "pledge" in r.get("category", "").lower()), None
    )

    if not pledge_row:
        return page_result(page, {"pledge_row_found": False, "report": (
            f"## {symbol} — Promoter Pledge\n\n"
            "No dedicated pledge row found in Screener.in's shareholding table for this company.\n\n"
            "This usually means **promoter shares are not pledged** — Screener only shows the "
            "line when pledging exists. To be certain, cross-check the 'Pledged percentage' "
            "field via `screen_stocks(\"Pledged percentage > 0\")` filtered to this symbol, or "
            "the company's own shareholding pattern (SAST) filings."
        )})

    vals = pledge_row.get("values", [])[-len(quarters):]
    vals_padded = vals + [""] * (len(quarters) - len(vals))

    lines = [
        f"## {symbol} — Promoter Pledge History (%)",
        "",
        f"{'Quarter':<12} " + "  ".join(f"{q:>10}" for q in quarters),
        f"{'Pledged %':<12} " + "  ".join(f"{v:>10}" for v in vals_padded),
    ]

    def _pct(v: str) -> float | None:
        try:
            return float(str(v).replace("%", "").strip())
        except (ValueError, TypeError):
            return None

    numeric = [(_pct(v)) for v in vals if _pct(v) is not None]
    if numeric:
        latest = numeric[-1]
        severity = (
            "**High severity** — over 50% of promoter holding is pledged, a significant risk."
            if latest > 50
            else "**Moderate concern** — meaningful pledge exists; watch for further increases."
            if latest > 20
            else "**Low concern** — pledge level is modest."
            if latest > 0
            else "No pledge currently."
        )
        trend = (
            "rising" if len(numeric) >= 2 and numeric[-1] > numeric[0] + 0.5
            else "falling" if len(numeric) >= 2 and numeric[-1] < numeric[0] - 0.5
            else "stable"
        )
        lines += ["", f"**Latest pledge**: {latest:.1f}% — {severity}", f"**Trend**: {trend}"]

    return page_result(page, {"pledge_row_found": True, "report": "\n".join(lines)})


async def get_peers(symbol: str, financial_type: FinancialType = "consolidated") -> ToolResult:
    client = await get_client()
    page = await fetch_company_page(symbol, financial_type)
    symbol, html = page.symbol, page.html

    # Screener.in's real peer table is loaded client-side via an AJAX call
    # keyed on the company's "warehouse id" (distinct from its numeric id):
    #   GET /api/company/{warehouse_id}/peers/
    # Fetch that endpoint directly instead of relying on the initial page load.
    warehouse_id = parse_warehouse_id(html)
    peers: list[dict[str, str]] = []
    if warehouse_id:
        ajax_html = await client.get_html(f"/api/company/{warehouse_id}/peers/")
        peers = parse_peers_ajax(ajax_html)

    if not peers:
        peers = parse_peers(html)

    if not peers:
        return page_result(
            page,
            {"report": f"No peer data found for {symbol}. Use `compare_companies([\"SYMBOL1\", \"SYMBOL2\"])` to compare specific companies side-by-side."},
            missing=["peers"],
            reason="Screener.in's peer-comparison endpoint returned no rows.",
        )

    # Check if we only got sector breadcrumb context (no actual peer rows)
    if peers[0].get("_note"):
        lines = [f"## {symbol} — Peer Comparison", "", peers[0]["_note"], ""]
        sector_rows = [p for p in peers[1:] if "Sector Level" in p]
        for r in sector_rows:
            lines.append(f"  {r['Sector Level']}: {r['Name']}")
        lines.append(
            "\nTo compare peers manually, use `compare_companies([\"SYMBOL1\", \"SYMBOL2\", ...])`."
        )
        return page_result(
            page, {"report": "\n".join(lines)},
            missing=["peers"],
            reason="Peer table unavailable — only sector context could be extracted.",
        )

    columns = [c for c in peers[0].keys() if not c.startswith("_")]
    col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in peers)) for c in columns}

    header = "  ".join(f"{c:{col_widths[c]}}" for c in columns)
    separator = "  ".join("-" * col_widths[c] for c in columns)
    lines = [f"## {symbol} — Peer Comparison", "", header, separator]

    for row in peers:
        lines.append("  ".join(f"{str(row.get(c, '')):{col_widths[c]}}" for c in columns))

    return page_result(page, {"report": "\n".join(lines)})


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

    companies = []
    failed = []
    missing_all = []
    for requested, r in zip(symbols, results):
        if isinstance(r, Exception):
            entry = {"requested_symbol": requested, "error": str(r)}
            if isinstance(r, ToolError) and r.details.get("candidates"):
                entry["candidates"] = r.details["candidates"]
            failed.append(entry)
            continue
        page, ov = r
        warnings += page.warnings
        missing = overview_missing_fields(ov)
        if missing:
            warnings.append(f"{page.symbol}: no value on source page for {', '.join(missing)}.")
            missing_all += [f"{page.symbol}.{m}" for m in missing]
        companies.append((page.symbol, ov))

    if not companies:
        raise ToolError(
            "Could not fetch data for any of the requested companies.",
            "symbol_not_found",
            failed=failed,
        )
    for f in failed:
        hint = f" Candidates: {', '.join(c['symbol'] for c in f['candidates'])}." if f.get("candidates") else ""
        warnings.append(f"Could not fetch {f['requested_symbol']}: {f['error']}{hint}")

    # Build comparison table
    metrics_order = [
        "Market Cap", "Current Price", "Stock P/E", "Price to Book value",
        "Return on capital employed", "Return on equity",
        "Dividend Yield", "Debt to equity", "Sales growth 5Years",
        "Profit growth 5Years", "ROCE 5Year",
    ]

    lines = ["# Company Comparison", ""]
    header = f"{'Metric':<35} " + "  ".join(f"{sym:>15}" for sym, _ in companies)
    lines.append(header)
    lines.append("-" * len(header))

    # Key ratios
    all_ratio_keys = set()
    for _, data in companies:
        all_ratio_keys.update(data.get("key_ratios", {}).keys())

    # Show ordered metrics first, then remaining
    shown = set()
    for metric in metrics_order:
        for key in all_ratio_keys:
            if metric.lower() in key.lower() and key not in shown:
                row = f"{key:<35} " + "  ".join(
                    f"{data.get('key_ratios', {}).get(key) or '—':>15}" for _, data in companies
                )
                lines.append(row)
                shown.add(key)

    for key in sorted(all_ratio_keys - shown):
        row = f"{key:<35} " + "  ".join(
            f"{data.get('key_ratios', {}).get(key) or '—':>15}" for _, data in companies
        )
        lines.append(row)

    # Sectors
    lines += ["", "**Sectors:**"]
    for sym, data in companies:
        sectors = ", ".join(data.get("sectors", []))
        lines.append(f"- {sym}: {sectors or '—'}")

    return ToolResult(
        data={"symbols": [sym for sym, _ in companies], "failed": failed, "report": "\n".join(lines)},
        warnings=warnings,
        missing_fields=missing_all,
        partial=bool(failed or missing_all),
        reason="Some companies could not be fetched or had blank fields." if (failed or missing_all) else None,
    )
