"""
Stock screening tools — translate natural-language intent into Screener.in queries
and return formatted results with analyst commentary.
"""

import asyncio
import re
from typing import Optional

from ..core.company_page import fetch_company_page
from ..core.envelope import ToolError, ToolResult
from ..core.indices import resolve_index
from ..core.numbers import to_number
from ..parsers.company import parse_overview
from .research_tools import relative_valuation_for
from .technical_tools import (
    DEFAULT_MAX_CANDIDATES,
    fetch_candidates,
    parse_query,
    run_technical_screen,
)

# ─── Pre-built themes ─────────────────────────────────────────────────────────
# Two kinds:
#   * "query" themes are a Screener query run across the whole market.
#   * sector themes have a "universe" — the companies actually in the sector,
#     from NSE index constituents, Screener industry pages, or a curated list
#     where neither exists — and "filters" applied to those companies' rows.
#     Screener's query language has no industry field, so a sector can't be
#     expressed as a query; running a generic growth screen and calling it
#     "defense" returned mostly non-defense stocks.
#
# Sector filters may only use columns the universe pages carry: index and
# industry tables have Market Capitalization, Price to Earning, Return on
# equity, Profit growth 5Years, Dividend yield, ...; curated lists are read
# from company pages (Market Capitalization, Price to Earning, Return on
# equity, Return on capital employed, Dividend yield).

_IND = {
    "aerospace_defense": "/market/IN07/IN0702/IN070201/IN070201001/",
    "shipbuilding": "/market/IN07/IN0702/IN070204/IN070204006/",
    "railway_wagons": "/market/IN07/IN0702/IN070204/IN070204005/",
    "specialty_chemicals": "/market/IN01/IN0101/IN010101/IN010101002/",
}
CURATED_AS_OF = "2026-09"

THEMES: dict[str, dict] = {
    "undervalued_small_cap": {
        "description": "Small caps (< ₹5000 Cr) with ROCE > 15%, low debt, P/E < 20",
        "query": "Market Capitalization < 5000 AND Return on capital employed > 15 AND Debt to equity < 0.5 "
                 "AND Profit growth 3Years > 10 AND Price to Earning < 20",
    },
    "high_roce_low_debt": {
        "description": "ROCE > 20%, debt-to-equity < 0.3, 5-year profit growth > 12%",
        "query": "Return on capital employed > 20 AND Debt to equity < 0.3 AND Profit growth 5Years > 12",
    },
    "compounders": {
        "description": "15%+ five-year sales and profit growth with ROE and ROCE > 15%, low debt",
        "query": "Sales growth 5Years > 15 AND Profit growth 5Years > 15 AND Return on equity > 15 "
                 "AND Debt to equity < 0.5 AND Return on capital employed > 15",
    },
    "turnaround": {
        "description": "Strong recent profit recovery on growing sales",
        "query": "Profit growth 3Years > 25 AND Profit growth last year > 20 AND Sales growth 3Years > 10 "
                 "AND Return on capital employed > 10",
    },
    "rising_profit_falling_price": {
        "description": "Profits up 15%+ a year over 3 years while the share price fell over the last year, P/E < 15",
        "query": "Profit growth 3Years > 15 AND Sales growth 3Years > 10 AND Return over 1year < 0 "
                 "AND Price to Earning < 15",
    },
    "improving_roce": {
        "description": "ROCE > 15% and higher than both last year's and its 5-year average",
        "query": "Return on capital employed > 15 AND Return on capital employed > Return on capital employed "
                 "preceding year AND Return on capital employed > Average return on capital employed 5Years "
                 "AND Debt to equity < 1",
    },
    "hidden_gems": {
        "description": "Small cap (< ₹5000 Cr), ROCE > 15%, 5-year sales growth > 15%, low debt",
        "query": "Market Capitalization < 5000 AND Return on capital employed > 15 AND Sales growth 5Years > 15 "
                 "AND Debt to equity < 0.5",
    },
    "dividend_aristocrats": {
        "description": ("Yield > 2%, a dividend paid in each of the last two years and a 3-year average payout "
                        "> 20%, with ROE > 12% and low debt (Screener has no longer dividend-streak field)"),
        "query": "Dividend yield > 2 AND Dividend last year > 0 AND Dividend preceding year > 0 AND "
                 "Average dividend payout 3years > 20 AND Return on equity > 12 AND Debt to equity < 0.5",
    },
    "qarp": {
        "description": "Quality at a reasonable price: P/E < 25, ROE > 15%, profit growth > 12%, low debt, > ₹1000 Cr",
        "query": "Price to Earning < 25 AND Return on equity > 15 AND Profit growth 5Years > 12 "
                 "AND Debt to equity < 0.5 AND Market Capitalization > 1000",
    },
    "micro_cap_growth": {
        "description": "Micro caps (< ₹1000 Cr) with 20%+ three-year sales and profit growth, ROCE > 15%",
        "query": "Market Capitalization < 1000 AND Sales growth 3Years > 20 AND Profit growth 3Years > 20 "
                 "AND Return on capital employed > 15",
    },
    # ── sector themes ──
    "defense": {
        "description": "Defence companies (Nifty India Defence + Screener's Aerospace & Defense and "
                       "Shipbuilding industries) with ROE > 12% and profit growth",
        "universe": [("index", "defence"), ("industry", _IND["aerospace_defense"]), ("industry", _IND["shipbuilding"])],
        "filters": "Market Capitalization > 500 AND Return on equity > 12 AND Profit growth 5Years > 10",
    },
    "ev_theme": {
        "description": "Nifty EV & New Age Automotive constituents (EV makers, batteries, auto components) "
                       "with ROE > 12%",
        "universe": [("index", "ev")],
        "filters": "Market Capitalization > 1000 AND Return on equity > 12",
    },
    "chemicals": {
        "description": "Specialty chemicals (Screener's Specialty Chemicals industry + Nifty Chemicals) with "
                       "ROE > 15% and 5-year profit growth > 12%",
        "universe": [("industry", _IND["specialty_chemicals"]), ("index", "chemicals")],
        "filters": "Market Capitalization > 500 AND Return on equity > 15 AND Profit growth 5Years > 12",
    },
    "railways": {
        "description": "Railway companies (Screener's Railway Wagons industry + a curated list of railway "
                       "PSUs and suppliers) with ROE > 12%",
        "universe": [("industry", _IND["railway_wagons"]),
                     ("symbols", ["RVNL", "IRCON", "IRFC", "RAILTEL", "IRCTC", "RITES", "CONCOR", "BEML",
                                  "HBLENGINE", "KERNEX"])],
        "filters": "Market Capitalization > 500 AND Return on equity > 12",
    },
    "renewable_energy": {
        "description": "Renewable energy (curated list: wind/solar equipment makers and green power producers) "
                       "with ROE > 10%",
        "universe": [("symbols", ["SUZLON", "INOXWIND", "WAAREEENER", "PREMIERENE", "NTPCGREEN", "ACMESOLAR",
                                  "KPIGREEN", "ADANIGREEN", "JSWENERGY", "TATAPOWER", "BORORENEW", "SWSOLAR",
                                  "WEBELSOLAR", "INOXGREEN"])],
        "filters": "Market Capitalization > 500 AND Return on equity > 10",
    },
}


def theme_catalog() -> str:
    """Every theme with its criteria — used as the screen_by_theme docstring."""
    lines = []
    for key, t in THEMES.items():
        crit = t.get("query") or f"sector universe; filters: {t['filters']}"
        lines.append(f"      {key} — {t['description']}\n          {crit}")
    return "\n".join(lines)


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
    dy = num("Dividend yield")
    if dy is not None and dy > 25:
        flags.append(f"dividend yield of {dy:g}% — a special dividend or stale price, not a recurring yield")
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
    page: int = 1,
) -> ToolResult:
    """
    Run a Screener.in query, optionally mixed with technical clauses, with
    AND / OR / parentheses. `page` pages through results `limit` at a time.
    """
    limit = max(1, min(int(limit or 25), 200))
    page = max(1, int(page or 1))
    offset = (page - 1) * limit
    groups = parse_query(query)
    warnings: list[str] = []
    guard = f"Market Capitalization > {min_market_cap:g}" if min_market_cap and not _mentions_mcap(query) else None

    try:
        if any(tech for _, tech in groups):
            merged: dict[str, dict] = {}
            partial_reasons, per_group = [], []
            sources, filters = [], []
            guard_used = False
            for gi, (fundamental, technical) in enumerate(groups, 1):
                fund = fundamental + ([guard] if guard and fundamental else [])
                guard_used = guard_used or bool(guard and fundamental)
                res = await run_technical_screen(
                    fund, technical, universe=universe, limit=10_000,
                    max_candidates=max_candidates, sort_by=sort_by, order=order,
                )
                d = res.data
                sources.append(d["source"])
                filters.append(d["technical_filters"])
                per_group.append({"group": gi, "candidates_available": d["candidates_available"],
                                  "candidates_scanned": d["candidates_scanned"], "matches": d.get("matches_found")})
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
            # Groups over the same source scan the same stocks (price history is
            # cached, so re-checking costs nothing) — don't double-count them.
            same_source = all(src == sources[0] for src in sources)
            agg = max if same_source else sum
            data = {
                "query": query,
                "or_groups": len(groups),
                "source": sources[0] if same_source else sources,
                "technical_filters": filters[0] if len(filters) == 1 else filters,
                "candidates_available": agg((g["candidates_available"] or 0) for g in per_group),
                "candidates_scanned": agg(g["candidates_scanned"] for g in per_group),
            }
            if len(groups) > 1:
                data["groups"] = per_group
            partial = bool(partial_reasons)
            reason = " ".join(partial_reasons) or None
        else:
            screener_query = query if not guard else (
                f"({query}) AND {guard}" if re.search(r"\bOR\b", query, re.I) else f"{query} AND {guard}")
            rows, total = await fetch_candidates(
                query=screener_query, max_rows=offset + limit + (25 if exclude_flagged else 0),
                sort=sort_by, order=order)
            data = {"query": query, "screener_query": screener_query, "total_matches": total}
            partial, reason = False, None
            guard_used = bool(guard)
    except PermissionError:
        raise ToolError(
            _LOGIN_REQUIRED_MSG.format(query=query).strip(),
            "login_required",
            query=query,
            hint="Technical-only queries (e.g. 'RSI < 30 AND Price above 200 DMA') run without "
                 "login against an index universe such as nifty500.",
        )

    if guard_used:
        warnings.append(f"Added '{guard}' to filter out micro-caps — pass min_market_cap=0 to disable, "
                        "or put your own Market Capitalization clause in the query.")
    rows, excluded = _apply_hygiene(rows, exclude_flagged, min_market_cap if guard else 0)
    if excluded:
        warnings.append(f"Excluded {len(excluded)} result(s) with implausible numbers (see "
                        "excluded_for_data_quality) — pass exclude_flagged=False to keep them, flagged.")
        data["excluded_for_data_quality"] = excluded[:25]

    if any(tech for _, tech in groups):
        data["matches_found"] = len(rows)  # after hygiene, before the limit cut
    available = len(rows)
    rows = rows[offset:offset + limit]
    data["page"] = page
    if offset and not rows:
        warnings.append(f"Page {page} is past the end of the results ({available} after filtering).")
    if peer_relative and rows:
        await _attach_peer_relative(rows, warnings)
    if not rows:
        if any(tech for _, tech in groups):
            warnings.append(
                f"No stocks passed the technical filters among the {data.get('candidates_scanned', 0)} scanned — "
                "loosen the thresholds or try a wider universe."
            )
        else:
            warnings.append(
                "No companies matched. Screener query syntax uses field names like "
                "`Market Capitalization`, `Return on capital employed`, `Debt to equity`, "
                "`Profit growth 5Years`, `Price to Earning`."
            )
    data.update({"showing": len(rows), "results": rows})
    return ToolResult(data=data, warnings=warnings, partial=partial, reason=reason)


_CLAUSE_RE = re.compile(r"^\s*(.+?)\s*(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$")
_OPS = {">": float.__gt__, "<": float.__lt__, ">=": float.__ge__, "<=": float.__le__, "=": float.__eq__}


def parse_filters(expr: str) -> list[tuple[str, str, float]]:
    """'Market Capitalization > 500 AND Return on equity > 12' → [(field, op, value), ...]."""
    out = []
    for clause in re.split(r"\s+AND\s+", expr.strip(), flags=re.I):
        m = _CLAUSE_RE.match(clause)
        if not m:
            raise ValueError(f"Unsupported sector-theme filter clause: {clause!r}")
        out.append((m.group(1), m.group(2), float(m.group(3))))
    return out


def _passes(fundamentals: dict, filters: list[tuple[str, str, float]]) -> tuple[bool, list[str]]:
    """(passes, fields missing from the row). A row missing a field fails —
    it can't be shown to meet a criterion it has no number for."""
    missing = [f for f, _, _ in filters if not isinstance(fundamentals.get(f), (int, float))]
    if missing:
        return False, missing
    return all(_OPS[op](float(fundamentals[f]), v) for f, op, v in filters), []


_PAGE_FIELDS = {  # company-page top ratio → screen column name
    "Market Cap": "Market Capitalization", "Stock P/E": "Price to Earning", "ROE": "Return on equity",
    "ROCE": "Return on capital employed", "Dividend Yield": "Dividend yield", "Book Value": "Book value",
}


async def _symbol_rows(symbols: list[str], warnings: list[str]) -> list[dict]:
    async def one(sym: str) -> dict:
        page = await fetch_company_page(sym, "consolidated")
        ov = parse_overview(page.html)
        f = {"Current Price": to_number(ov.get("current_price"))}
        f.update({out: to_number(ov.get("key_ratios", {}).get(src)) for src, out in _PAGE_FIELDS.items()})
        return {"symbol": page.symbol, "name": ov.get("name"), "company_id": page.company_id, "fundamentals": f}

    results = await asyncio.gather(*[one(s) for s in symbols], return_exceptions=True)
    failed = [s for s, r in zip(symbols, results) if isinstance(r, Exception)]
    if failed:
        warnings.append(f"Couldn't fetch {len(failed)} curated symbol(s): {', '.join(failed)}.")
    return [r for r in results if not isinstance(r, Exception)]


async def _universe_rows(universe: list[tuple], warnings: list[str]) -> tuple[list[dict], list[dict]]:
    """Rows for every company in a sector universe, de-duplicated → (rows, sources)."""
    rows: dict[str, dict] = {}
    sources = []
    for kind, ref in universe:
        if kind == "index":
            key, slug, _, display = resolve_index(ref)
            got, _ = await fetch_candidates(index_slug=slug, max_rows=500)
            sources.append({"type": "nse_index", "name": display, "companies": len(got)})
        elif kind == "industry":
            got, _ = await fetch_candidates(page_path=ref, max_rows=500)
            sources.append({"type": "screener_industry", "url": f"https://www.screener.in{ref}", "companies": len(got)})
        else:
            got = await _symbol_rows(ref, warnings)
            sources.append({"type": "curated_list", "as_of": CURATED_AS_OF, "symbols": ref, "companies": len(got)})
        for r in got:
            rows.setdefault(r["company_id"] or r["symbol"], r)
    return list(rows.values()), sources


async def _sector_screen(key: str, theme: dict, limit: int, page: int) -> ToolResult:
    warnings: list[str] = []
    filters = parse_filters(theme["filters"])
    universe, sources = await _universe_rows(theme["universe"], warnings)

    matched, missing_counts = [], {}
    for r in universe:
        ok, missing = _passes(r.get("fundamentals") or {}, filters)
        for f in missing:
            missing_counts[f] = missing_counts.get(f, 0) + 1
        if ok:
            matched.append(r)
    if missing_counts:
        warnings.append("Excluded for lacking a filter field on the source page: "
                        + ", ".join(f"{n} without {f}" for f, n in missing_counts.items()) + ".")
    matched, excluded = _apply_hygiene(matched, exclude_flagged=True)
    matched.sort(key=lambda r: (r.get("fundamentals") or {}).get("Market Capitalization") or 0, reverse=True)
    offset = (max(1, page) - 1) * limit
    data = {
        "theme": key,
        "description": theme["description"],
        "universe": sources,
        "universe_size": len(universe),
        "filters": theme["filters"],
        "total_matches": len(matched),
        "page": page,
        "sorted_by": "Market Capitalization (desc)",
    }
    if excluded:
        data["excluded_for_data_quality"] = excluded[:25]
        warnings.append(f"Excluded {len(excluded)} result(s) with implausible numbers (see excluded_for_data_quality).")
    rows = matched[offset:offset + limit]
    if not rows:
        warnings.append(f"No companies in the {len(universe)}-company universe passed the filters on page {page}.")
    data.update({"showing": len(rows), "results": rows})
    return ToolResult(data=data, warnings=warnings)


async def screen_by_theme(theme: str, limit: int = 20, page: int = 1) -> ToolResult:
    """Run a pre-built theme (see THEMES / theme_catalog for the criteria)."""
    key = _match_theme(theme)
    if not key:
        raise ToolError(
            f"Theme '{theme}' not recognized.",
            "invalid_input",
            available_themes={k: t["description"] for k, t in THEMES.items()},
        )
    t = THEMES[key]
    limit = max(1, min(int(limit or 20), 200))
    page = max(1, int(page or 1))
    if "universe" in t:
        return await _sector_screen(key, t, limit, page)
    result = await screen_stocks(t["query"], limit=limit, page=page)
    result.data = {"theme": key, "description": t["description"], **result.data}
    return result


def _match_theme(theme: str) -> Optional[str]:
    """Fuzzy match a theme name to a theme key."""
    theme_lower = theme.lower().strip().replace(" ", "_").replace("-", "_")
    theme_lower = {"defence": "defense", "ev": "ev_theme", "renewables": "renewable_energy",
                   "railway": "railways"}.get(theme_lower, theme_lower)
    if theme_lower in THEMES:
        return theme_lower
    for key in THEMES:
        if theme_lower in key or key in theme_lower:
            return key
    words = set(theme_lower.split("_"))
    for key in THEMES:
        if words & set(key.split("_")):
            return key
    return None
