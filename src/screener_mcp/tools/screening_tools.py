"""
Stock screening tools — translate natural-language intent into Screener.in queries
and return formatted results with analyst commentary.
"""

import asyncio
import re
from typing import Optional

from ..core.envelope import ToolError, ToolResult
from .research_tools import relative_valuation_for
from .technical_tools import (
    DEFAULT_MAX_CANDIDATES,
    fetch_candidates,
    parse_query,
    run_technical_screen,
)

# ─── Pre-built query templates ─────────────────────────────────────────────────
# These map natural language themes to Screener query strings.

QUERY_TEMPLATES = {
    # Quality & Value
    "undervalued_small_cap": (
        "Market Capitalization < 5000 AND "
        "Return on capital employed > 15 AND "
        "Debt to equity < 0.5 AND "
        "Profit growth 3Years > 10 AND "
        "Price to Earning < 20"
    ),
    "high_roce_low_debt": (
        "Return on capital employed > 20 AND "
        "Debt to equity < 0.3 AND "
        "Profit growth 5Years > 12"
    ),
    "compounders": (
        "Sales growth 5Years > 15 AND "
        "Profit growth 5Years > 15 AND "
        "Return on equity > 15 AND "
        "Debt to equity < 0.5 AND "
        "Return on capital employed > 15"
    ),
    "turnaround": (
        "Profit growth 3Years > 25 AND "
        "Profit growth last year > 20 AND "
        "Sales growth 3Years > 10 AND "
        "Return on capital employed > 10"
    ),
    "rising_profit_falling_price": (
        "Profit growth 3Years > 15 AND "
        "Sales growth 3Years > 10 AND "
        "Price to Earning < 15"
    ),
    "improving_roce": (
        "Return on capital employed > 15 AND "
        "Profit growth 5Years > 12 AND "
        "Debt to equity < 1"
    ),
    "hidden_gems": (
        "Market Capitalization < 5000 AND "
        "Return on capital employed > 15 AND "
        "Sales growth 5Years > 15 AND "
        "Debt to equity < 0.5"
    ),
    "dividend_aristocrats": (
        "Dividend yield > 2 AND "
        "Profit growth 5Years > 8 AND "
        "Return on equity > 12 AND "
        "Debt to equity < 0.5"
    ),

    # Sector themes
    "ev_theme": (
        "Sales growth 3Years > 15 AND "
        "Debt to equity < 1"
        # User should filter by sector manually; Screener doesn't have EV-tag filter
    ),
    "chemicals": (
        "Debt to equity < 0.5 AND "
        "Profit growth 5Years > 15 AND "
        "Return on capital employed > 15 AND "
        "Sales growth 5Years > 12"
    ),
    "defense": (
        "Sales growth 3Years > 15 AND "
        "Return on capital employed > 12"
    ),
    "railways": (
        "Sales growth 3Years > 15 AND "
        "Profit growth 3Years > 20 AND "
        "Debt to equity < 1"
    ),
    "renewable_energy": (
        "Sales growth 3Years > 15 AND "
        "Debt to equity < 2"
    ),

    # Quality at reasonable price
    "qarp": (
        "Price to Earning < 25 AND "
        "Return on equity > 15 AND "
        "Profit growth 5Years > 12 AND "
        "Debt to equity < 0.5 AND "
        "Market Capitalization > 1000"
    ),

    # Micro caps with momentum
    "micro_cap_growth": (
        "Market Capitalization < 1000 AND "
        "Sales growth 3Years > 20 AND "
        "Profit growth 3Years > 20 AND "
        "Return on capital employed > 15"
    ),
}

THEME_DESCRIPTIONS = {
    "ev_theme": "EV & Auto ancillary companies with strong growth",
    "chemicals": "Specialty chemicals with low debt and strong growth",
    "defense": "Defense sector with revenue momentum",
    "railways": "Railway infra/equipment with profit growth",
    "renewable_energy": "Renewable energy companies with revenue growth",
    "undervalued_small_cap": "Small caps (< ₹5000 Cr) with high ROCE, low debt",
    "high_roce_low_debt": "High ROCE (>20%) companies with minimal debt",
    "compounders": "Classic compounders: 15%+ growth on all fronts",
    "turnaround": "Turnaround stories with strong recent recovery",
    "rising_profit_falling_price": "Improving profits with low PE (potential value)",
    "improving_roce": "Companies with ROCE >15% and profit momentum",
    "hidden_gems": "Hidden gems: small cap, high ROCE, strong growth",
    "dividend_aristocrats": "Consistent dividend payers with quality financials",
    "qarp": "Quality at reasonable price (QARP)",
    "micro_cap_growth": "High-growth micro caps (< ₹1000 Cr)",
}


_LOGIN_REQUIRED_MSG = """
**Login required for stock screening.**

Stock screening on Screener.in requires a free account. To enable it:

1. Register for free at https://www.screener.in/register/
2. Set these environment variables before starting the MCP server:
   ```
   SCREENER_USERNAME=your@email.com
   SCREENER_PASSWORD=yourpassword
   ```
3. Restart the MCP server.

**Query you tried**: `{query}`

**What you can do now (no login needed)**:
- `search_company("name")` — search for any company
- `get_company_overview("SYMBOL")` — full key ratios
- `get_financials("SYMBOL")` — P&L, balance sheet, cash flow
- `get_quarterly_results("SYMBOL")` — last 8 quarters
- `get_shareholding_pattern("SYMBOL")` — promoter/FII/DII trends
- `compare_companies(["ITC", "HINDUNILVR"])` — side-by-side comparison
"""


# ─── result hygiene ───────────────────────────────────────────────────────────
# Broad screens sorted by growth used to be dominated by tiny, illiquid names
# with one-off numbers (e.g. EPS 370 on a ₹0.10 Cr market cap). Two guards:
#   1. a minimum market cap added to the Screener query unless the query
#      already constrains market cap;
#   2. per-row sanity checks; flagged rows are dropped (and listed) by default.

DEFAULT_MIN_MARKET_CAP = 100  # ₹ Cr


def sanity_flags(fundamentals: dict) -> list[str]:
    f = fundamentals or {}
    def num(key):
        v = f.get(key)
        return v if isinstance(v, (int, float)) else None
    flags = []
    pe, mcap, price = num("Price to Earning"), num("Market Capitalization"), num("Current Price")
    profit, sales = num("Net Profit latest quarter"), num("Sales latest quarter")
    growth = num("YOY Quarterly profit growth")
    if pe is not None and 0 < pe < 1:
        flags.append("P/E below 1 — earnings probably one-off or misreported")
    if growth is not None and growth > 500 and (mcap or 0) < 1000:
        flags.append("quarterly profit up >500% on a small base — likely a one-off")
    if profit is not None and sales is not None and sales > 0 and profit > sales:
        flags.append("quarterly profit exceeds sales — other income / exceptional item")
    if sales is not None and sales <= 0.5 and (mcap or 0) < 500:
        flags.append("negligible sales (≤ ₹0.5 Cr last quarter)")
    if price is not None and price < 1:
        flags.append("price below ₹1")
    return flags


def _mentions_mcap(query: str) -> bool:
    return bool(re.search(r"market\s+capitali[sz]ation", query or "", re.I))


def _apply_hygiene(rows: list[dict], exclude_flagged: bool, min_market_cap: float = 0,
                   fundamentals_key: str = "fundamentals"):
    kept, excluded = [], []
    for r in rows:
        f = r.get(fundamentals_key) or {}
        flags = sanity_flags(f)
        mcap = f.get("Market Capitalization")
        # backstop for the query-side guard (e.g. when the source ignores it)
        if min_market_cap and isinstance(mcap, (int, float)) and mcap < min_market_cap:
            flags.append(f"market cap ₹{mcap:g} Cr below the ₹{min_market_cap:g} Cr minimum")
        if flags:
            r["data_quality_flags"] = flags
        if flags and exclude_flagged:
            excluded.append({"symbol": r.get("symbol"), "name": r.get("name"), "flags": flags})
        else:
            kept.append(r)
    return kept, excluded


async def _attach_peer_relative(rows: list[dict], warnings: list[str], cap: int = 20):
    targets = rows[:cap]
    results = await asyncio.gather(*[relative_valuation_for(r["symbol"]) for r in targets], return_exceptions=True)
    failed = 0
    for r, rel in zip(targets, results):
        if isinstance(rel, Exception):
            failed += 1
            continue
        r["relative_valuation"] = {k: rel[k] for k in (
            "industry", "industry_median_pe", "pe_vs_industry_pct", "industry_median_roce",
            "roce_vs_industry_pp", "assessment") if k in rel}
    if len(rows) > cap:
        warnings.append(f"Peer-relative valuation added for the first {cap} results only.")
    if failed:
        warnings.append(f"Peer-relative valuation unavailable for {failed} result(s).")


async def screen_stocks(
    query: str,
    sort_by: str = "",
    order: str = "desc",
    limit: int = 25,
    universe: str = "",
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    min_market_cap: float = DEFAULT_MIN_MARKET_CAP,
    exclude_flagged: bool = True,
    peer_relative: bool = False,
) -> ToolResult:
    """
    Run a Screener.in query, optionally mixed with technical clauses, with
    AND / OR / parentheses.
    """
    limit = max(1, min(int(limit or 25), 200))
    groups = parse_query(query)
    warnings: list[str] = []
    guard = f"Market Capitalization > {min_market_cap:g}" if min_market_cap and not _mentions_mcap(query) else None

    try:
        if any(tech for _, tech in groups):
            merged: dict[str, dict] = {}
            partial_reasons, stats = [], {"candidates_available": 0, "candidates_scanned": 0}
            sources, filters = [], []
            for gi, (fundamental, technical) in enumerate(groups, 1):
                fund = fundamental + ([guard] if guard and fundamental else [])
                res = await run_technical_screen(
                    fund, technical, universe=universe, limit=10_000,
                    max_candidates=max_candidates, sort_by=sort_by, order=order,
                )
                d = res.data
                sources.append(d["source"])
                filters.append(d["technical_filters"])
                stats["candidates_available"] += d["candidates_available"] or 0
                stats["candidates_scanned"] += d["candidates_scanned"]
                for w in res.warnings:
                    if w not in warnings:
                        warnings.append(w)
                if res.partial and res.reason:
                    partial_reasons.append(f"group {gi}: {res.reason}" if len(groups) > 1 else res.reason)
                for m in d["results"]:
                    entry = merged.setdefault(m["company_id"], {**m, "matched_groups": []})
                    entry["matched_groups"].append(gi)
            # Groups with no fundamental clauses scan an index universe (large caps
            # already), so the market-cap guard is only added to Screener-query groups.
            rows = list(merged.values())
            data = {
                "query": query,
                "or_groups": len(groups),
                "source": sources[0] if len(sources) == 1 else sources,
                "technical_filters": filters[0] if len(filters) == 1 else filters,
                **stats,
            }
            partial = bool(partial_reasons)
            reason = " ".join(partial_reasons) or None
        else:
            screener_query = query if not guard else (
                f"({query}) AND {guard}" if re.search(r"\bOR\b", query, re.I) else f"{query} AND {guard}")
            rows, total = await fetch_candidates(
                query=screener_query, max_rows=limit + (25 if exclude_flagged else 0), sort=sort_by, order=order)
            data = {"query": query, "screener_query": screener_query, "total_matches": total}
            partial, reason = False, None
    except PermissionError:
        raise ToolError(
            _LOGIN_REQUIRED_MSG.format(query=query).strip(),
            "login_required",
            query=query,
            hint="Technical-only queries (e.g. 'RSI < 30 AND Price above 200 DMA') run without "
                 "login against an index universe such as nifty500.",
        )

    if guard:
        warnings.append(f"Added '{guard}' to filter out micro-caps — pass min_market_cap=0 to disable, "
                        "or put your own Market Capitalization clause in the query.")
    rows, excluded = _apply_hygiene(rows, exclude_flagged, min_market_cap if guard else 0)
    if excluded:
        warnings.append(f"Excluded {len(excluded)} result(s) with implausible numbers (see "
                        "excluded_for_data_quality) — pass exclude_flagged=False to keep them, flagged.")
        data["excluded_for_data_quality"] = excluded[:25]

    rows = rows[:limit]
    if peer_relative and rows:
        await _attach_peer_relative(rows, warnings)
    if not rows:
        warnings.append(
            "No companies matched. Screener query syntax uses field names like "
            "`Market Capitalization`, `Return on capital employed`, `Debt to equity`, "
            "`Profit growth 5Years`, `Price to Earning`."
        )
    data.update({"showing": len(rows), "results": rows})
    if "matches_found" not in data and any(tech for _, tech in groups):
        data["matches_found"] = len(rows)
    return ToolResult(data=data, warnings=warnings, partial=partial, reason=reason)


async def screen_by_theme(theme: str, limit: int = 20) -> ToolResult:
    """
    Run a pre-built thematic screen.

    Available themes:
      undervalued_small_cap, high_roce_low_debt, compounders, turnaround,
      rising_profit_falling_price, improving_roce, hidden_gems,
      dividend_aristocrats, qarp, micro_cap_growth,
      ev_theme, chemicals, defense, railways, renewable_energy
    """
    # Fuzzy match theme
    theme_key = _match_theme(theme)
    if not theme_key:
        raise ToolError(
            f"Theme '{theme}' not recognized.",
            "invalid_input",
            available_themes=THEME_DESCRIPTIONS,
        )

    query = QUERY_TEMPLATES[theme_key]
    result = await screen_stocks(query, limit=limit)
    result.data = {"theme": theme_key, "description": THEME_DESCRIPTIONS[theme_key], **result.data}
    return result


async def list_themes() -> str:
    """List all available pre-built screening themes."""
    lines = ["## Available Investment Themes", ""]
    for key, desc in THEME_DESCRIPTIONS.items():
        q = QUERY_TEMPLATES[key]
        lines.append(f"### `{key}`")
        lines.append(f"{desc}")
        lines.append(f"```\n{q}\n```")
        lines.append("")
    return "\n".join(lines)


def _match_theme(theme: str) -> Optional[str]:
    """Fuzzy match a theme name to a template key."""
    theme_lower = theme.lower().replace(" ", "_").replace("-", "_")
    if theme_lower in QUERY_TEMPLATES:
        return theme_lower
    for key in QUERY_TEMPLATES:
        if theme_lower in key or key in theme_lower:
            return key
    # Partial word match
    words = set(theme_lower.split("_"))
    for key in QUERY_TEMPLATES:
        key_words = set(key.split("_"))
        if words & key_words:
            return key
    return None
