"""
Company data tools — each function is called by an MCP tool.
They fetch the Screener.in page, parse it, and return formatted text
that Claude can read naturally.
"""

import json
from typing import Literal

from ..client import get_client
from ..parsers.company import (
    parse_overview,
    parse_profit_loss,
    parse_balance_sheet,
    parse_cash_flow,
    parse_quarterly_results,
    parse_ratios,
    parse_shareholding,
    parse_peers,
    parse_full_page,
)
from ..parsers.screener import parse_search_results


FinancialType = Literal["consolidated", "standalone"]


def _fmt_yearly(data: dict, title: str, n_years: int = 5) -> str:
    """Format a yearly financial table into readable text."""
    years = data.get("years", [])[-n_years:]
    rows = data.get("rows", [])
    if not years or not rows:
        return f"No {title} data available."

    lines = [f"### {title}", "", f"{'Metric':<35} " + "  ".join(f"{y:>10}" for y in years)]
    lines.append("-" * (35 + 13 * len(years)))

    for row in rows:
        label = row.get("label", "")
        values = row.get("values", [])[-n_years:]
        values_padded = values + [""] * (len(years) - len(values))
        lines.append(f"{label:<35} " + "  ".join(f"{v:>10}" for v in values_padded))

    return "\n".join(lines)


async def search_company(query: str) -> str:
    """Search for a company by name or NSE/BSE symbol."""
    client = await get_client()
    results = await client.get_json("/api/company/search/", params={"q": query})
    parsed = parse_search_results(results if isinstance(results, list) else [])

    if not parsed:
        return f"No companies found matching '{query}'. Try the full company name or stock symbol."

    lines = [f"Found {len(parsed)} result(s) for '{query}':", ""]
    for i, r in enumerate(parsed[:10], 1):
        lines.append(f"{i}. **{r['name']}** — symbol: `{r['screener_id']}`")

    lines.append("\nUse the symbol (e.g. `TCS`, `INFY`) with other tools to get detailed data.")
    return "\n".join(lines)


async def get_company_overview(symbol: str, financial_type: FinancialType = "consolidated") -> str:
    """Get a comprehensive overview of a company."""
    client = await get_client()
    path = f"/company/{symbol.upper()}/"
    if financial_type == "consolidated":
        path += "consolidated/"
    html = await client.get_html(path)
    data = parse_overview(html)

    ratios = data.get("key_ratios", {})
    sectors = ", ".join(data.get("sectors", [])) or "—"

    def r(key):
        return ratios.get(key, "—")

    lines = [
        f"# {data['name']}",
        f"**NSE**: {data.get('nse_code') or '—'}  |  **BSE**: {data.get('bse_code') or '—'}  |  **Sector**: {sectors}",
        f"**Current Price**: {data.get('current_price') or '—'}  |  **52W High**: {data.get('52_week_high') or '—'}  |  **52W Low**: {data.get('52_week_low') or '—'}",
        "",
        "## Key Metrics",
        f"| Metric | Value |",
        f"|--------|-------|",
    ]
    for k, v in ratios.items():
        lines.append(f"| {k} | {v} |")

    if data.get("about"):
        lines += ["", "## About", data["about"]]

    return "\n".join(lines)


async def get_financials(
    symbol: str,
    statement: Literal["profit_loss", "balance_sheet", "cash_flow", "ratios"] = "profit_loss",
    financial_type: FinancialType = "consolidated",
    years: int = 5,
) -> str:
    client = await get_client()
    path = f"/company/{symbol.upper()}/"
    if financial_type == "consolidated":
        path += "consolidated/"
    html = await client.get_html(path)

    parsers = {
        "profit_loss": (parse_profit_loss, "Profit & Loss (₹ Crore)"),
        "balance_sheet": (parse_balance_sheet, "Balance Sheet (₹ Crore)"),
        "cash_flow": (parse_cash_flow, "Cash Flow (₹ Crore)"),
        "ratios": (parse_ratios, "Key Ratios History"),
    }
    fn, title = parsers[statement]
    data = fn(html)
    return _fmt_yearly(data, f"{symbol.upper()} — {title} [{financial_type}]", years)


async def get_quarterly_results(symbol: str, financial_type: FinancialType = "consolidated") -> str:
    client = await get_client()
    path = f"/company/{symbol.upper()}/"
    if financial_type == "consolidated":
        path += "consolidated/"
    html = await client.get_html(path)
    data = parse_quarterly_results(html)

    years = data.get("years", [])[-8:]
    rows = data.get("rows", [])
    if not years or not rows:
        return f"No quarterly results available for {symbol.upper()}."

    lines = [
        f"## {symbol.upper()} — Quarterly Results (₹ Crore) [{financial_type}]",
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

    return "\n".join(lines)


async def get_shareholding(symbol: str) -> str:
    client = await get_client()
    path = f"/company/{symbol.upper()}/"
    html = await client.get_html(path)
    data = parse_shareholding(html)

    quarters = data.get("quarters", [])[-8:]
    rows = data.get("rows", [])
    if not quarters or not rows:
        return f"No shareholding data found for {symbol.upper()}."

    lines = [
        f"## {symbol.upper()} — Shareholding Pattern (%)",
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

    return "\n".join(lines)


async def get_peers(symbol: str, financial_type: FinancialType = "consolidated") -> str:
    client = await get_client()
    path = f"/company/{symbol.upper()}/"
    if financial_type == "consolidated":
        path += "consolidated/"
    html = await client.get_html(path)
    peers = parse_peers(html)

    if not peers:
        return f"No peer data found for {symbol.upper()}."

    if not peers:
        return "No peers found."

    columns = list(peers[0].keys()) if peers else []
    col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in peers)) for c in columns}

    header = "  ".join(f"{c:{col_widths[c]}}" for c in columns)
    separator = "  ".join("-" * col_widths[c] for c in columns)
    lines = [f"## {symbol.upper()} — Peer Comparison", "", header, separator]

    for row in peers:
        lines.append("  ".join(f"{str(row.get(c, '')):{col_widths[c]}}" for c in columns))

    return "\n".join(lines)


async def compare_companies(symbols: list[str], financial_type: FinancialType = "consolidated") -> str:
    """Side-by-side comparison of 2-5 companies."""
    import asyncio

    if len(symbols) < 2:
        return "Please provide at least 2 company symbols to compare."
    if len(symbols) > 5:
        symbols = symbols[:5]

    client = await get_client()

    async def fetch(sym: str):
        path = f"/company/{sym.upper()}/"
        if financial_type == "consolidated":
            path += "consolidated/"
        html = await client.get_html(path)
        return sym.upper(), parse_overview(html)

    results = await asyncio.gather(*[fetch(s) for s in symbols], return_exceptions=True)

    companies = []
    for r in results:
        if isinstance(r, Exception):
            continue
        companies.append(r)

    if not companies:
        return "Could not fetch data for any of the requested companies."

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
                    f"{data.get('key_ratios', {}).get(key, '—'):>15}" for _, data in companies
                )
                lines.append(row)
                shown.add(key)

    for key in sorted(all_ratio_keys - shown):
        row = f"{key:<35} " + "  ".join(
            f"{data.get('key_ratios', {}).get(key, '—'):>15}" for _, data in companies
        )
        lines.append(row)

    # Sectors
    lines += ["", "**Sectors:**"]
    for sym, data in companies:
        sectors = ", ".join(data.get("sectors", []))
        lines.append(f"- {sym}: {sectors or '—'}")

    return "\n".join(lines)
