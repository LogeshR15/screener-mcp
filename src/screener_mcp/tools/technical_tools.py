"""
Technical / price-action screening and sector-relative context.

Screener.in's query language only covers fundamentals, so technical clauses
("52 week low distance < 10", "RSI < 30", "Price above 200 DMA",
"Volume vs 20 day average > 2") are split out of a screen_stocks query and
evaluated here against each candidate's price history (core/technicals.py).
The fundamental remainder still goes to Screener as a normal screen; with no
fundamental clauses the candidates come from a public NSE index constituents
list instead, so technical-only screens need no Screener login.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..client import get_client
from ..core.company_page import fetch_company_page
from ..core.envelope import ToolError, ToolResult
from ..core.indices import INDICES, resolve_index, sector_index_for
from ..core.numbers import to_number
from ..core.quality import is_financial, overview_field, overview_missing_fields
from ..core.technicals import HISTORY_DAYS, compute_technicals, fetch_price_history
from ..parsers.company import debt_to_equity, parse_overview
from ..parsers.screener import parse_screen_results

DEFAULT_UNIVERSE = "nifty500"
DEFAULT_MAX_CANDIDATES = 150
MAX_CANDIDATES_CAP = 500

# ─── technical query clauses ───────────────────────────────────────────────────

# Human-facing names for each metric, used in docs and results.
TECH_METRICS = {
    "pct_above_52w_low": "% above 52-week low",
    "pct_below_52w_high": "% below 52-week high",
    "rsi14": "RSI (14)",
    "volume_vs_20d_avg": "volume ÷ 20-day average volume",
    "pct_vs_dma20": "% above(+)/below(−) 20 DMA",
    "pct_vs_dma50": "% above(+)/below(−) 50 DMA",
    "pct_vs_dma200": "% above(+)/below(−) 200 DMA",
}

# field aliases, matched against the normalised clause LHS
_FIELD_ALIASES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(52w low distance|distance (from|to) 52w low|(%|pct|percent) above 52w low|above 52w low( %)?)$"), "pct_above_52w_low"),
    (re.compile(r"^(52w high distance|distance (from|to) 52w high|(%|pct|percent) below 52w high|below 52w high( %)?)$"), "pct_below_52w_high"),
    (re.compile(r"^rsi( 14)?$"), "rsi14"),
    (re.compile(r"^(volume (vs|/|to|over) 20d (average|avg)( volume)?|volume ratio|relative volume|volume spike)$"), "volume_vs_20d_avg"),
    (re.compile(r"^(price|current price) (vs|to|from) (200|50|20) dma$"), "pct_vs_dma"),
    (re.compile(r"^(distance (from|to) )?(200|50|20) dma distance$"), "pct_vs_dma"),
]

_OPS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "=": lambda a, b: abs(a - b) < 1e-9,
}
_OP_WORDS = {"above": ">", "below": "<", "over": ">", "under": "<"}

_NUMERIC_CLAUSE = re.compile(r"^(?P<lhs>.+?)\s*(?P<op>>=|<=|>|<|=)\s*(?P<num>-?\d+(\.\d+)?)\s*%?$")
_LEVEL = r"(price|current price|(?:dma ?(?:200|50|20))|(?:(?:200|50|20) ?dma))"
_LEVEL_CLAUSE = re.compile(rf"^(?P<lhs>{_LEVEL})\s*(?P<op>>=|<=|>|<|above|below|over|under)\s*(?P<rhs>{_LEVEL})$")


@dataclass
class TechFilter:
    metric: str  # key in the technicals dict, or "level" for level-vs-level
    op: str
    value: Optional[float]
    text: str
    lhs: Optional[str] = None  # for level comparisons: "price" | "dma20" | ...
    rhs: Optional[str] = None

    def describe(self) -> str:
        return self.text

    def evaluate(self, t: dict) -> Optional[bool]:
        """True/False, or None if the metric couldn't be computed."""
        if self.metric == "level":
            a, b = t.get(self.lhs), t.get(self.rhs)
            if a is None or b is None:
                return None
            return _OPS[self.op](a, b)
        v = t.get(self.metric)
        if v is None:
            return None
        return _OPS[self.op](v, self.value)


def _normalise(clause: str) -> str:
    c = clause.strip().lower()
    c = re.sub(r"rsi\s*\(\s*14\s*\)", "rsi 14", c)
    c = re.sub(r"\b52\s*-?\s*(weeks?|wks?|w)\b", "52w", c)
    c = re.sub(r"\b(200|50|20)\s*-?\s*(days?|d)\b(\s*(moving average|ma|sma))?", r"\1d", c)
    c = re.sub(r"\b(200|50|20)d\s+(dma|moving average|sma|ma)\b", r"\1 dma", c)
    c = re.sub(r"\bdma\s*(200|50|20)\b", r"\1 dma", c)
    c = re.sub(r"\b(200|50|20)d\b(?! (average|avg))", r"\1 dma", c)
    c = re.sub(r"\s+", " ", c)
    return c


def _level_key(text: str) -> str:
    text = text.strip()
    if text in ("price", "current price"):
        return "price"
    return "dma" + re.search(r"(200|50|20)", text).group(1)


def parse_technical_clause(clause: str) -> Optional[TechFilter]:
    """Return a TechFilter if ``clause`` is technical, else None (fundamental)."""
    c = _normalise(clause)

    m = _LEVEL_CLAUSE.match(c)
    if m:
        lhs, rhs = _level_key(m.group("lhs")), _level_key(m.group("rhs"))
        if lhs == rhs:
            return None
        op = _OP_WORDS.get(m.group("op"), m.group("op"))
        return TechFilter("level", op, None, clause.strip(), lhs=lhs, rhs=rhs)

    m = _NUMERIC_CLAUSE.match(c)
    if not m:
        return None
    lhs = m.group("lhs").strip()
    for pattern, metric in _FIELD_ALIASES:
        pm = pattern.match(lhs)
        if pm:
            if metric == "pct_vs_dma":
                metric = f"pct_vs_dma{re.search(r'(200|50|20)', lhs).group(1)}"
            return TechFilter(metric, m.group("op"), float(m.group("num")), clause.strip())
    return None


def split_query(query: str) -> tuple[list[str], list[TechFilter]]:
    """Split a screen query into (fundamental clauses, technical filters)."""
    query = (query or "").strip()
    if not query:
        return [], []
    clauses = [c for c in re.split(r"\s+AND\s+", query, flags=re.I) if c.strip()]
    fundamental, technical = [], []
    for clause in clauses:
        tf = parse_technical_clause(clause)
        if tf:
            technical.append(tf)
        else:
            fundamental.append(clause.strip())
    if technical and any(re.search(r"\bOR\b", c, re.I) or "(" in c for c in fundamental):
        raise ToolError(
            "Technical clauses can only be combined with AND at the top level. Move OR / "
            "parenthesised fundamental groups into a separate screen, or drop them.",
            "invalid_query",
        )
    return fundamental, technical


# ─── candidate fetching ────────────────────────────────────────────────────────

def _row_to_candidate(row: dict) -> Optional[dict]:
    cid = row.get("_company_id")
    url = row.get("_url", "")
    m = re.search(r"/company/((?:id/)?[^/]+)/", url)
    if not cid or not m:
        return None
    columns = {}
    for k, v in row.items():
        if k.startswith("_") or k in ("S.No.", "Name", "Company"):
            continue
        n = to_number(v)
        columns[k] = n if n is not None else (v or None)
    return {
        "symbol": m.group(1).upper(),
        "name": row.get("Name") or row.get("Company"),
        "company_id": cid,
        "fundamentals": columns,
    }


async def fetch_candidates(
    query: Optional[str] = None,
    index_slug: Optional[str] = None,
    max_rows: int = DEFAULT_MAX_CANDIDATES,
    sort: str = "",
    order: str = "",
) -> tuple[list[dict], Optional[int]]:
    """Rows from a Screener screen (``query``) or an index constituents page.

    Returns (candidates, total rows available at the source). Screen queries
    need a Screener login (raises PermissionError otherwise); index pages are
    public. Pages come 25 rows at a time; after the first page the rest are
    fetched concurrently (the client caps concurrency and retries 429s).
    """
    client = await get_client()

    async def page(n: int) -> dict:
        if query:
            params = {"query": query, "page": str(n)}
            if sort:
                params.update({"sort": sort, "order": order or "desc"})
            html = await client.get_html("/screen/raw/", params=params)
        else:
            html = await client.get_html(f"/company/{index_slug}/", params={"page": str(n)} if n > 1 else None)
        return parse_screen_results(html)

    first = await page(1)
    rows = list(first.get("companies", []))
    total = first.get("total_results")
    total_pages = first.get("total_pages") or 1
    per_page = max(len(rows), 1)
    needed_pages = min(total_pages, -(-max_rows // per_page))
    if needed_pages > 1:
        rest = await asyncio.gather(*[page(n) for n in range(2, needed_pages + 1)])
        for d in rest:
            rows += d.get("companies", [])

    seen, out = set(), []
    for row in rows:
        cand = _row_to_candidate(row)
        if cand and cand["company_id"] not in seen:
            seen.add(cand["company_id"])
            out.append(cand)
    return out[:max_rows], total


async def _technicals_for(cands: list[dict]) -> tuple[dict[str, dict], dict[str, list[str]], list[str]]:
    """company_id → metrics, company_id → warnings, [failed symbols]."""
    async def one(c):
        hist = await fetch_price_history(c["company_id"])
        return compute_technicals(hist)

    results = await asyncio.gather(*[one(c) for c in cands], return_exceptions=True)
    metrics, notes, failed = {}, {}, []
    for c, r in zip(cands, results):
        if isinstance(r, Exception) or not r[0]:
            failed.append(c["symbol"])
            continue
        metrics[c["company_id"]], notes[c["company_id"]] = r
    return metrics, notes, failed


# ─── the screen engine ─────────────────────────────────────────────────────────

_SORT_ALIASES = {**{k: k for k in TECH_METRICS}, "rsi": "rsi14", "volume": "volume_vs_20d_avg",
                 "52w low": "pct_above_52w_low", "52w high": "pct_below_52w_high"}


def _default_sort(filters: list[TechFilter]) -> tuple[str, bool]:
    """Sort by the first numeric technical filter: '<' → ascending, '>' → descending."""
    for f in filters:
        if f.metric != "level":
            return f.metric, f.op in (">", ">=")
    for f in filters:
        # "Price above 200 DMA" → furthest above first; "below" → furthest below first
        if f.lhs == "price" and f.rhs and f.rhs.startswith("dma"):
            return f"pct_vs_{f.rhs}", f.op in (">", ">=")
    return "pct_above_52w_low", False


async def run_technical_screen(
    fundamental: list[str],
    technical: list[TechFilter],
    universe: str = "",
    limit: int = 25,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    sort_by: str = "",
    order: str = "",
    candidate_filter=None,
) -> ToolResult:
    warnings: list[str] = []
    max_candidates = max(1, min(int(max_candidates or DEFAULT_MAX_CANDIDATES), MAX_CANDIDATES_CAP))
    fundamental_query = " AND ".join(fundamental)

    if fundamental_query:
        source = {"type": "screener_query", "query": fundamental_query}
        if universe:
            warnings.append(
                f"universe='{universe}' ignored — fundamental clauses were given, so candidates "
                "come from the Screener query across the whole market."
            )
        candidates, total = await fetch_candidates(query=fundamental_query, max_rows=max_candidates)
    else:
        idx = resolve_index(universe or DEFAULT_UNIVERSE)
        if not idx:
            raise ToolError(
                f"Unknown universe '{universe}'. Choose one of: {', '.join(INDICES)}.",
                "invalid_input",
            )
        key, slug, _, display = idx
        source = {"type": "index_constituents", "universe": key, "index": display}
        candidates, total = await fetch_candidates(index_slug=slug, max_rows=max_candidates)

    if candidate_filter:
        candidates = [c for c in candidates if candidate_filter(c)]

    metrics, notes, failed = await _technicals_for(candidates)

    matches, insufficient = [], []
    for c in candidates:
        t = metrics.get(c["company_id"])
        if t is None:
            continue
        verdicts = [f.evaluate(t) for f in technical]
        if any(v is None for v in verdicts):
            insufficient.append(c["symbol"])
            continue
        if all(verdicts):
            entry = {**c, "technicals": t}
            if notes.get(c["company_id"]):
                entry["warnings"] = notes[c["company_id"]]
            matches.append(entry)

    key = _SORT_ALIASES.get((sort_by or "").strip().lower())
    reverse = (order or "desc").lower() != "asc"
    if key:
        def sort_value(m): return m["technicals"].get(key)
    elif sort_by:
        def sort_value(m): return m["fundamentals"].get(sort_by)
    else:
        key, reverse = _default_sort(technical)
        def sort_value(m): return m["technicals"].get(key)
    sortable = [m for m in matches if isinstance(sort_value(m), (int, float))]
    unsortable = [m for m in matches if not isinstance(sort_value(m), (int, float))]
    matches = sorted(sortable, key=sort_value, reverse=reverse) + unsortable

    scanned = len(candidates)
    partial = False
    reason = None
    if total and total > scanned and not candidate_filter:
        partial = True
        reason = (
            f"Only the first {scanned} of {total} candidates were checked against the technical "
            "filters. Tighten the fundamental clauses, pick a smaller universe, or raise max_candidates."
        )
    if failed:
        partial = True
        reason = (reason + " " if reason else "") + f"Price history could not be fetched for {len(failed)} candidate(s)."
        warnings.append(f"No price history for: {', '.join(failed[:20])}{' …' if len(failed) > 20 else ''}")
    if insufficient:
        warnings.append(
            f"{len(insufficient)} candidate(s) excluded because a filter metric couldn't be computed "
            f"(e.g. too little history for a 200 DMA): {', '.join(insufficient[:10])}{' …' if len(insufficient) > 10 else ''}"
        )

    return ToolResult(
        data={
            "source": source,
            "technical_filters": [
                {"clause": f.text, "metric": f.metric if f.metric != "level" else f"{f.lhs} {f.op} {f.rhs}",
                 "op": f.op, "value": f.value}
                for f in technical
            ],
            "candidates_available": total,
            "candidates_scanned": scanned,
            "matches_found": len(matches),
            "results": matches[: max(1, int(limit or 25))],
            "technicals_basis": "daily closes from Screener.in price chart; 52W range is close-based",
        },
        warnings=warnings,
        partial=partial,
        reason=reason,
    )


# ─── 52-week-low candidates ────────────────────────────────────────────────────

async def _enrich(match: dict) -> dict:
    """Add the same clean overview fields get_company_overview returns."""
    page = await fetch_company_page(match["symbol"], "consolidated")
    ov = parse_overview(page.html)
    missing = overview_missing_fields(ov)
    financial = is_financial(ov.get("sectors", []))
    match["overview"] = {
        "name": ov.get("name"),
        "financial_type": page.financial_type,
        "sectors": ov.get("sectors", []),
        "current_price": to_number(overview_field(ov, "current_price")),
        "52_week_high": to_number(overview_field(ov, "52_week_high")),
        "52_week_low": to_number(overview_field(ov, "52_week_low")),
        "market_cap_cr": to_number(overview_field(ov, "market_cap")),
        "pe": to_number(overview_field(ov, "pe")),
        "roce": to_number(overview_field(ov, "roce")),
        "roe": to_number(overview_field(ov, "roe")),
        "book_value": to_number(overview_field(ov, "book_value")),
        "dividend_yield": to_number(overview_field(ov, "dividend_yield")),
        "debt_to_equity": None if financial else debt_to_equity(page.html),
        "debt_to_equity_basis": (
            "not meaningful for banks/NBFCs/insurers — leverage is the business model"
            if financial else "Borrowings ÷ (Equity Capital + Reserves), latest balance sheet"
        ),
        "is_financial": financial,
    }
    if missing:
        match["overview"]["missing_fields"] = missing
    return match


async def get_52_week_low_candidates(
    min_roce: float = 15,
    max_debt_to_equity: float = 0.5,
    max_pct_above_52w_low: float = 10,
    min_market_cap: float = 1000,
    universe: str = "",
    limit: int = 20,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> ToolResult:
    client = await get_client()
    tech = [TechFilter("pct_above_52w_low", "<=", float(max_pct_above_52w_low),
                       f"52 week low distance <= {max_pct_above_52w_low:g}")]
    warnings: list[str] = []

    if client.logged_in and not universe:
        fundamental = [
            f"Return on capital employed > {min_roce:g}",
            f"Debt to equity < {max_debt_to_equity:g}",
            f"Market Capitalization > {min_market_cap:g}",
        ]
        result = await run_technical_screen(fundamental, tech, limit=max(limit * 2, limit),
                                            max_candidates=max_candidates)
        de_prefiltered = True
    else:
        if not client.logged_in and not universe:
            warnings.append(
                f"No Screener login — scanning the {INDICES[DEFAULT_UNIVERSE][2]} constituents instead "
                "of the whole market. Set SCREENER_USERNAME/SCREENER_PASSWORD for a market-wide scan."
            )

        def quality(c):
            f = c["fundamentals"]
            roce, mcap = f.get("Return on capital employed"), f.get("Market Capitalization")
            return (roce is None or roce > min_roce) and (mcap is None or mcap > min_market_cap)

        result = await run_technical_screen([], tech, universe=universe or DEFAULT_UNIVERSE,
                                            limit=max(limit * 3, limit), max_candidates=max_candidates,
                                            candidate_filter=quality)
        de_prefiltered = False

    matches = result.data["results"]
    enriched = await asyncio.gather(*[_enrich(m) for m in matches], return_exceptions=True)
    final, dropped_de, unknown_de, financials = [], [], [], []
    for m in enriched:
        if isinstance(m, Exception):
            continue
        de = m["overview"]["debt_to_equity"]
        if m["overview"]["is_financial"]:
            financials.append(m["symbol"])
        elif not de_prefiltered:
            if de is None:
                unknown_de.append(m["symbol"])
            elif de > max_debt_to_equity:
                dropped_de.append(m["symbol"])
                continue
        m["pct_above_52w_low"] = m["technicals"]["pct_above_52w_low"]
        final.append(m)

    if financials:
        warnings.append(
            f"{', '.join(financials)}: financial companies — debt-to-equity filter not applied (leverage is "
            "their business). Judge them on ROE, asset quality (GNPA) and capital adequacy instead."
        )
    if unknown_de:
        warnings.append(
            f"Debt-to-equity couldn't be computed for {', '.join(unknown_de)} (e.g. banks/NBFCs, or no "
            "balance sheet) — kept, but check leverage manually."
        )
    result.warnings = warnings + result.warnings
    result.data["results"] = final[:limit]
    result.data["matches_found"] = len(final)
    result.data["criteria"] = {
        "min_roce": min_roce,
        "max_debt_to_equity": max_debt_to_equity,
        "max_pct_above_52w_low": max_pct_above_52w_low,
        "min_market_cap_cr": min_market_cap,
    }
    if dropped_de:
        result.data["excluded_for_debt"] = dropped_de
    return result


# ─── sector / index relative performance ──────────────────────────────────────

def _close_on_or_before(series: dict[str, float], dates_sorted: list[str], target: str) -> Optional[tuple[str, float]]:
    best = None
    for d in dates_sorted:
        if d <= target:
            best = d
        else:
            break
    return (best, series[best]) if best else None


def _window_return(dates: list[str], closes: list[float], start: str) -> Optional[float]:
    series = dict(zip(dates, closes))
    hit = _close_on_or_before(series, dates, start)
    if not hit or not hit[1]:
        return None
    return (closes[-1] / hit[1] - 1) * 100


def _beta_corr(stock: dict[str, float], bench: dict[str, float], dates: list[str]) -> tuple[Optional[float], Optional[float]]:
    common = [d for d in dates if d in stock and d in bench]
    rs, rb = [], []
    for prev, cur in zip(common, common[1:]):
        if stock[prev] and bench[prev]:
            rs.append(stock[cur] / stock[prev] - 1)
            rb.append(bench[cur] / bench[prev] - 1)
    if len(rs) < 10:
        return None, None
    ms, mb = sum(rs) / len(rs), sum(rb) / len(rb)
    cov = sum((a - ms) * (b - mb) for a, b in zip(rs, rb)) / (len(rs) - 1)
    var_b = sum((b - mb) ** 2 for b in rb) / (len(rb) - 1)
    var_s = sum((a - ms) ** 2 for a in rs) / (len(rs) - 1)
    if not var_b or not var_s:
        return None, None
    return round(cov / var_b, 2), round(cov / (var_b ** 0.5 * var_s ** 0.5), 2)


def _verdict(stock: float, sector: Optional[float], market: Optional[float]) -> str:
    if sector is None:
        return "Sector return unavailable — compare against the market figure only."
    diff = stock - sector
    band = max(2.0, 0.25 * abs(sector))
    direction = "fell" if stock < 0 else "rose"
    if abs(diff) <= band:
        text = f"Stock {direction} roughly in line with its sector ({diff:+.1f} pp) — the move looks sector/market-driven rather than company-specific."
    elif diff < 0:
        text = (f"Stock underperformed its sector by {abs(diff):.1f} pp — points to company-specific weakness"
                if stock < 0 else f"Stock lagged its sector by {abs(diff):.1f} pp despite rising")
        text += "."
    else:
        text = (f"Stock held up better than its sector by {diff:.1f} pp — relative strength"
                if stock < 0 or sector < 0 else f"Stock outperformed its sector by {diff:.1f} pp — company-specific strength")
        text += "."
    if market is not None:
        text += f" Sector vs Nifty 50: {sector - market:+.1f} pp."
    return text


async def compare_to_sector(symbol: str, days: int = 30, benchmark: str = "") -> ToolResult:
    days = max(5, min(int(days or 30), 365))
    page = await fetch_company_page(symbol, "consolidated")
    ov = parse_overview(page.html)
    warnings = list(page.warnings)

    if benchmark:
        sector = resolve_index(benchmark)
        if not sector:
            raise ToolError(f"Unknown benchmark '{benchmark}'. Choose one of: {', '.join(INDICES)}.", "invalid_input")
    else:
        sector = sector_index_for(ov.get("sectors", []))
        if not sector:
            sector = resolve_index("nifty500")
            warnings.append(
                f"No sector index maps to {ov.get('sectors') or 'this company'} — using Nifty 500 as the "
                "benchmark. Pass benchmark=... to choose one."
            )
    market = resolve_index("nifty50")

    if not page.company_id:
        raise ToolError(f"Couldn't find {page.symbol}'s chart id on Screener.in.", "upstream_error")
    fetch_days = HISTORY_DAYS
    series = await asyncio.gather(
        fetch_price_history(page.company_id, fetch_days),
        fetch_price_history(sector[2], fetch_days),
        fetch_price_history(market[2], fetch_days),
        return_exceptions=True,
    )
    stock_h, sector_h, market_h = series
    if isinstance(stock_h, Exception) or not stock_h.closes:
        raise ToolError(f"No price history available for {page.symbol}.", "no_data")
    missing = []
    if isinstance(sector_h, Exception) or not sector_h.closes:
        sector_h, missing = None, missing + ["sector_return"]
    if isinstance(market_h, Exception) or not market_h.closes:
        market_h, missing = None, missing + ["market_return"]

    last = datetime.strptime(stock_h.dates[-1], "%Y-%m-%d")

    windows = sorted({7, 30, 90, days})
    rows = []
    for w in windows:
        if w > fetch_days - 5:
            continue
        start = (last - timedelta(days=w)).strftime("%Y-%m-%d")
        s = _window_return(stock_h.dates, stock_h.closes, start)
        sec = _window_return(sector_h.dates, sector_h.closes, start) if sector_h else None
        mkt = _window_return(market_h.dates, market_h.closes, start) if market_h else None
        rows.append({
            "window_days": w,
            "stock_return_pct": round(s, 2) if s is not None else None,
            "sector_return_pct": round(sec, 2) if sec is not None else None,
            "nifty50_return_pct": round(mkt, 2) if mkt is not None else None,
            "vs_sector_pp": round(s - sec, 2) if None not in (s, sec) else None,
            "vs_nifty50_pp": round(s - mkt, 2) if None not in (s, mkt) else None,
        })

    main = next(r for r in rows if r["window_days"] == days)
    start = (last - timedelta(days=days)).strftime("%Y-%m-%d")
    window_dates = [d for d in stock_h.dates if d >= start]
    beta = corr = None
    if sector_h:
        beta, corr = _beta_corr(dict(zip(stock_h.dates, stock_h.closes)),
                                dict(zip(sector_h.dates, sector_h.closes)), window_dates)
    window_closes = [c for d, c in zip(stock_h.dates, stock_h.closes) if d >= start]
    drawdown = round((window_closes[-1] / max(window_closes) - 1) * 100, 2) if window_closes else None

    verdict = (_verdict(main["stock_return_pct"], main["sector_return_pct"], main["nifty50_return_pct"])
               if main["stock_return_pct"] is not None else "Not enough history for this window.")

    return ToolResult(
        data={
            "symbol": page.symbol,
            "name": ov.get("name"),
            "sectors": ov.get("sectors", []),
            "sector_benchmark": {"key": sector[0], "index": sector[3]},
            "market_benchmark": {"key": market[0], "index": market[3]},
            "as_of": stock_h.dates[-1],
            "window_days": days,
            "returns": rows,
            "beta_vs_sector": beta,
            "correlation_vs_sector": corr,
            "drawdown_from_window_high_pct": drawdown,
            "verdict": verdict,
            "verdict_basis": (
                "Heuristic on relative return: within ±max(2pp, 25% of the sector move) counts as "
                "in line. Returns are close-to-close over calendar-day windows."
            ),
        },
        warnings=warnings,
        missing_fields=missing,
        reason="Benchmark price history unavailable." if missing else None,
        meta=page.meta,
    )

