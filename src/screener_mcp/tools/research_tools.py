"""
Research tools that go beyond trailing numeric screening:

  get_relative_valuation — P/E and ROCE vs the company's *industry* median,
                           for up to 20 stocks at once (industries are fetched
                           once and shared, so this scales across a screen)
  get_moat_signals       — quantitative moat proxies: revenue share / rank /
                           concentration in the industry, plus how durable
                           returns, margins and promoter holding have been
  get_forward_outlook    — forward-looking inputs: analyst EPS/revenue
                           estimates, order wins and capex/expansion filings,
                           and management guidance from the latest earnings
                           call (when the [ai] extra is installed)
"""

import asyncio
import re
import statistics
from datetime import datetime, timedelta
from typing import Optional

from ..core.company_page import CompanyPage, fetch_company_page, resolve_nse_symbol
from ..core.envelope import ToolError, ToolResult
from ..core.industry import fetch_industry, industry_stats, parse_classification, position_in, public_stats
from ..core.numbers import to_number
from ..core.nse_client import NSEError, get_nse_client
from ..core.quality import is_financial, overview_field
from ..core.yahoo import YahooError, get_yahoo_client, raw
from ..parsers.company import parse_overview, parse_profit_loss, parse_ratios, parse_shareholding

MAX_RELATIVE_SYMBOLS = 20


# ─── peer-relative valuation ──────────────────────────────────────────────────

def _valuation_label(pe_vs: Optional[float], roce_vs: Optional[float]) -> str:
    if pe_vs is None:
        return "P/E not comparable (loss-making, or no industry median)"
    if pe_vs <= -20:
        base = "cheaper than its industry"
    elif pe_vs >= 20:
        base = "at a premium to its industry"
    else:
        base = "valued in line with its industry"
    if roce_vs is not None:
        if pe_vs >= 20 and roce_vs >= 5:
            base += ", but the premium comes with clearly higher ROCE"
        elif pe_vs <= -20 and roce_vs <= -5:
            base += ", though ROCE is also clearly below peers (possible value trap)"
        elif pe_vs <= -20 and roce_vs >= 0:
            base += " with ROCE at or above peers"
    return base


async def relative_valuation_for(symbol: str) -> dict:
    page = await fetch_company_page(symbol, "consolidated")
    ov = parse_overview(page.html)
    cls = parse_classification(page.html)
    level = next((lvl for lvl in ("Industry", "Broad Industry", "Sector") if lvl in cls), None)
    if not level:
        raise ToolError(f"No industry classification on {page.symbol}'s Screener page.", "no_data")
    stats = industry_stats(await fetch_industry(cls[level]["url"]))
    pe = to_number(overview_field(ov, "pe"))
    roce = to_number(overview_field(ov, "roce"))
    med_pe, med_roce = stats["median_pe"], stats["median_roce"]
    pe_vs = round((pe / med_pe - 1) * 100, 1) if pe and med_pe else None
    roce_vs = round(roce - med_roce, 1) if roce is not None and med_roce is not None else None
    financial = is_financial(ov.get("sectors", []))
    out = {
        "symbol": page.symbol,
        "name": ov.get("name"),
        "industry": cls[level]["name"],
        "industry_companies": stats["companies"],
        "pe": pe,
        "industry_median_pe": med_pe,
        "pe_vs_industry_pct": pe_vs,
        "roce": roce,
        "industry_median_roce": med_roce,
        "roce_vs_industry_pp": roce_vs,
        "assessment": _valuation_label(pe_vs, None if financial else roce_vs),
    }
    if financial:
        out["note"] = "Financial company — compare on P/B and ROE; ROCE isn't meaningful for lenders."
    if page.warnings:
        out["warnings"] = page.warnings
    return out


async def get_relative_valuation(symbols: list[str] | str) -> ToolResult:
    if isinstance(symbols, str):
        symbols = [s for s in symbols.split(",")]
    symbols = [s.strip() for s in symbols if s and s.strip()]
    if not symbols:
        raise ToolError("Provide at least one symbol.", "invalid_input")
    warnings = []
    if len(symbols) > MAX_RELATIVE_SYMBOLS:
        warnings.append(f"Only the first {MAX_RELATIVE_SYMBOLS} of {len(symbols)} symbols were evaluated.")
        symbols = symbols[:MAX_RELATIVE_SYMBOLS]

    results = await asyncio.gather(*[relative_valuation_for(s) for s in symbols], return_exceptions=True)
    rows, failed = [], []
    for sym, r in zip(symbols, results):
        if isinstance(r, Exception):
            entry = {"requested_symbol": sym, "error": str(r)}
            if isinstance(r, ToolError) and r.details.get("candidates"):
                entry["candidates"] = r.details["candidates"]
            failed.append(entry)
        else:
            rows.append(r)
    if not rows:
        raise ToolError("Relative valuation failed for every symbol.", "no_data", failed=failed)
    for f in failed:
        warnings.append(f"{f['requested_symbol']}: {f['error']}")
    rows.sort(key=lambda r: (r["pe_vs_industry_pct"] is None, r["pe_vs_industry_pct"] or 0))
    return ToolResult(
        data={
            "results": rows,
            "failed": failed,
            "method": ("Industry = Screener's finest classification level. Medians use companies with "
                       "market cap ≥ ₹500 Cr and a positive P/E. Sorted cheapest vs industry first."),
        },
        warnings=warnings,
        partial=bool(failed),
        reason="Some symbols couldn't be evaluated." if failed else None,
    )


# ─── moat signals ─────────────────────────────────────────────────────────────

def _row_values(table: dict, label_prefix: str, drop_ttm: bool = True) -> list[float]:
    years = table.get("years", [])
    for row in table.get("rows", []):
        if row.get("label", "").lower().startswith(label_prefix.lower()):
            pairs = list(zip(years[-len(row["values"]):], row["values"]))
            return [to_number(v) for y, v in pairs
                    if to_number(v) is not None and not (drop_ttm and y.upper() == "TTM")]
    return []


def _durability(page: CompanyPage, financial: bool) -> dict:
    ratios = parse_ratios(page.html)
    pl = parse_profit_loss(page.html)
    roce = _row_values(ratios, "ROCE")
    opm = _row_values(pl, "OPM")
    sales = _row_values(pl, "Sales") or _row_values(pl, "Revenue")
    sh = parse_shareholding(page.html)
    promoter = []
    for row in sh.get("rows", []):
        if row.get("category", "").lower().startswith("promoter"):
            promoter = [to_number(v) for v in row.get("values", []) if to_number(v) is not None]
            break

    out: dict = {"years_of_history": len(sales)}
    if roce and not financial:
        out["roce"] = {
            "years": len(roce),
            "years_at_or_above_15pct": sum(1 for v in roce if v >= 15),
            "median": round(statistics.median(roce), 1),
            "min": min(roce),
            "stdev": round(statistics.pstdev(roce), 1) if len(roce) > 1 else None,
        }
    if opm and not financial:
        out["operating_margin"] = {
            "years": len(opm),
            "median": round(statistics.median(opm), 1),
            "min": min(opm), "max": max(opm),
            "stdev_pp": round(statistics.pstdev(opm), 1) if len(opm) > 1 else None,
        }
    if len(sales) >= 4 and sales[0] and sales[0] > 0 and sales[-1] > 0:
        n = len(sales) - 1
        out["sales_cagr_pct"] = {"years": n, "value": round(((sales[-1] / sales[0]) ** (1 / n) - 1) * 100, 1)}
    if promoter:
        out["promoter_holding"] = {
            "quarters": len(promoter), "latest_pct": promoter[-1],
            "change_pp": round(promoter[-1] - promoter[0], 2),
            "range_pp": round(max(promoter) - min(promoter), 2),
        }
    return out


async def get_moat_signals(symbol: str) -> ToolResult:
    page = await fetch_company_page(symbol, "consolidated")
    ov = parse_overview(page.html)
    financial = is_financial(ov.get("sectors", []))
    cls = parse_classification(page.html)
    warnings, missing = list(page.warnings), []

    position = industry = None
    level = next((lvl for lvl in ("Industry", "Broad Industry") if lvl in cls), None)
    if level:
        try:
            stats = industry_stats(await fetch_industry(cls[level]["url"]))
            position = position_in(stats, page.company_id)
            industry = {"level": level, "name": cls[level]["name"], **public_stats(stats)}
        except Exception as e:
            warnings.append(f"Industry data unavailable ({type(e).__name__}).")
            missing.append("industry_position")
    else:
        missing.append("industry_position")
    if level and position is None and "industry_position" not in missing:
        warnings.append("Company not found among the industry's top companies by quarterly sales "
                        "(small share, or no sales reported last quarter).")

    durability = _durability(page, financial)

    signals = {}
    if position:
        signals["market_leader"] = position["revenue_rank"] == 1
        signals["top_3_by_revenue"] = position["revenue_rank"] <= 3
    r = durability.get("roce")
    if r and r["years"] >= 5:
        signals["consistently_high_roce"] = r["years_at_or_above_15pct"] >= 0.8 * r["years"]
    m = durability.get("operating_margin")
    if m and m["years"] >= 5 and m["stdev_pp"] is not None:
        signals["stable_margins"] = m["stdev_pp"] <= 3
    p = durability.get("promoter_holding")
    if p and p["quarters"] >= 8:
        signals["stable_promoter_holding"] = p["range_pp"] <= 5
    if industry and industry.get("concentration"):
        signals["concentrated_industry"] = industry["concentration"] == "highly concentrated"

    if financial:
        warnings.append("Financial company — ROCE and operating-margin signals skipped (not meaningful for lenders).")

    return ToolResult(
        data={
            "symbol": page.symbol,
            "name": ov.get("name"),
            "classification": {k: v["name"] for k, v in cls.items()},
            "industry": industry,
            "position": position,
            "durability": durability,
            "signals": signals,
            "signals_present": f"{sum(1 for v in signals.values() if v)} of {len(signals)}",
            "thresholds": {
                "consistently_high_roce": "ROCE ≥ 15% in ≥ 80% of years (min 5 years)",
                "stable_margins": "operating-margin standard deviation ≤ 3 pp (min 5 years)",
                "stable_promoter_holding": "promoter stake range ≤ 5 pp over the quarters shown (min 8)",
                "concentrated_industry": "HHI > 2500 on quarterly sales across the industry",
            },
            "caveat": ("Quantitative proxies only. Revenue share is among listed companies on Screener "
                       "(unlisted, MNC-subsidiary and imported competitors are missing), and qualitative "
                       "moats — brand, switching costs, network effects, licences — still need judgement; "
                       "annual reports and earnings calls are the place to check them."),
        },
        warnings=warnings,
        missing_fields=missing,
        reason="Industry position couldn't be computed." if missing else None,
        meta=page.meta,
    )


# ─── forward outlook ──────────────────────────────────────────────────────────

_ORDER_RE = re.compile(
    r"\b(order|orders|contract|letter of (award|intent)|\bLoA\b|\bLoI\b|work order|purchase order|"
    r"bagged|secures?|wins?|awarded|empanel)", re.I)
# phrases that contain "order"/"win" but aren't order wins
_NOT_ORDERS_RE = re.compile(r"\bin order to\b|\border of\b|\bwinding\b", re.I)
_EXCLUDE_RE = re.compile(
    r"\b(court|tribunal|penalty|order dated|significant increase in volume|price movement|clarification)\b", re.I)
_CAPEX_RE = re.compile(
    r"\b(capex|capital expenditure|capacity (expansion|addition|enhancement)|new (plant|facility|unit)|"
    r"commission(ed|ing)|greenfield|brownfield|expansion)\b", re.I)
_AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?|million|mn|billion|bn)\b", re.I)
_UNIT_TO_CR = {"crore": 1, "crores": 1, "cr": 1, "cr.": 1, "lakh": 0.01, "lakhs": 0.01, "lac": 0.01,
               "lacs": 0.01, "million": 0.1, "mn": 0.1, "billion": 100, "bn": 100}


def _amount_cr(text: str) -> Optional[float]:
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return None
    value = to_number(m.group(1))
    factor = _UNIT_TO_CR.get(m.group(2).lower())
    return round(value * factor, 2) if value is not None and factor is not None else None


def _within(date_str: str, days: int) -> bool:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.now() - datetime.strptime(date_str.strip(), fmt) <= timedelta(days=days)
        except ValueError:
            continue
    return True


async def _estimates(ticker: str, price: Optional[float]) -> Optional[dict]:
    summary = await get_yahoo_client().quote_summary(ticker, ["earningsTrend"])
    trends = (summary or {}).get("earningsTrend", {}).get("trend", [])
    years = []
    today = datetime.now().strftime("%Y-%m-%d")
    for t in trends:
        if t.get("period") not in ("0y", "+1y"):
            continue
        # Uncovered stocks come back with placeholder zeros and a stale fiscal year.
        if t.get("endDate") and t["endDate"] < today:
            continue
        eps, rev = t.get("earningsEstimate") or {}, t.get("revenueEstimate") or {}
        eps_avg = raw(eps, "avg") or None
        if not eps_avg and not raw(rev, "avg"):
            continue
        years.append({
            "period": "current fiscal year" if t["period"] == "0y" else "next fiscal year",
            "fiscal_year_end": t.get("endDate"),
            "eps_estimate": round(eps_avg, 2) if eps_avg else None,
            "eps_low": raw(eps, "low"), "eps_high": raw(eps, "high"),
            "eps_growth_pct": round(raw(t, "growth") * 100, 1) if raw(t, "growth") is not None else None,
            "eps_analysts": raw(eps, "numberOfAnalysts"),
            "revenue_estimate_cr": round(raw(rev, "avg") / 1e7, 0) if raw(rev, "avg") else None,
            "revenue_growth_pct": round(raw(rev, "growth") * 100, 1) if raw(rev, "growth") is not None else None,
            "forward_pe": round(price / eps_avg, 1) if price and eps_avg and eps_avg > 0 else None,
        })
    return {"source": f"Yahoo Finance analyst estimates for {ticker}", "years": years} if years else None


async def _filings(nse_symbol: str, days: int) -> tuple[list[dict], list[dict]]:
    nse = await get_nse_client()
    items = await nse.get_announcements(nse_symbol)
    orders, capex = [], []
    for a in items:
        if not _within(a.get("date", ""), days):
            continue
        text = f"{a.get('category', '')} {a.get('headline', '')}"
        entry = {"date": a.get("date"), "headline": (a.get("headline") or a.get("category") or "")[:300],
                 "amount_cr": _amount_cr(text), "url": a.get("url") or None}
        if _ORDER_RE.search(_NOT_ORDERS_RE.sub(" ", text)) and not _EXCLUDE_RE.search(text):
            orders.append(entry)
        elif _CAPEX_RE.search(text):
            capex.append(entry)
    return orders, capex


_GUIDANCE_QUESTIONS = {
    "guidance": "What revenue, growth or margin guidance did management give for the coming year?",
    "order_book": "What is the current order book, order pipeline or backlog, and expected order inflows?",
    "capex": "What capex, capacity expansion or new plants did management announce, with amounts and timelines?",
}


async def _management_commentary(page: CompanyPage) -> dict:
    """Top excerpts from the latest earnings call for guidance / orders / capex.
    Raises ImportError if the [ai] extra isn't installed."""
    import importlib.util

    missing = [m for m in ("pdfplumber", "chromadb", "sentence_transformers") if importlib.util.find_spec(m) is None]
    if missing:
        raise ImportError(f"missing: {', '.join(missing)}")
    from ..core.rag import process_document, query_document
    from .documents import _parse_earnings_calls

    calls = _parse_earnings_calls(page.html)
    if not calls:
        return {"available": False, "note": "No earnings call transcripts listed on Screener.in."}
    call = calls[0]
    quarter = call.get("quarter", "latest").upper().replace(" ", "")
    name = f"{page.symbol}_{quarter}_transcript"
    status = await process_document(call["url"], name, extra_metadata={
        "symbol": page.symbol, "doc_type": "earnings_call", "label": quarter})
    if status["status"] == "error":
        return {"available": False, "note": f"Transcript couldn't be processed: {status.get('error')}"}
    answers = {}
    for key, question in _GUIDANCE_QUESTIONS.items():
        chunks = await query_document(name, question, top_k=2)
        answers[key] = [{"pages": c["metadata"].get("pages"), "relevance": c["score"], "excerpt": c["text"][:900]}
                        for c in chunks]
    return {"available": True, "call": call.get("title"), "transcript_url": call["url"],
            "index_freshness": status.get("freshness"), "excerpts": answers}


async def get_forward_outlook(symbol: str, days: int = 180, include_earnings_call: bool = True) -> ToolResult:
    days = max(30, min(int(days or 180), 730))
    page = await fetch_company_page(symbol, "consolidated")
    ov = parse_overview(page.html)
    price = to_number(ov.get("current_price"))
    nse, bse = (ov.get("nse_code") or "").upper(), ov.get("bse_code") or ""
    ticker = f"{nse}.NS" if nse else (f"{bse}.BO" if bse else None)
    warnings, missing = list(page.warnings), []

    async def estimates():
        return await _estimates(ticker, price) if ticker else None

    async def filings():
        if not nse:
            raise ToolError("Not listed on NSE — exchange filings unavailable.", "not_on_nse")
        sym, _, _ = await resolve_nse_symbol(nse)
        return await _filings(sym, days)

    async def commentary():
        return await _management_commentary(page) if include_earnings_call else None

    est, fil, com = await asyncio.gather(estimates(), filings(), commentary(), return_exceptions=True)

    if isinstance(est, YahooError):
        warnings.append(f"Analyst estimates unavailable: {est}.")
        est = None
        missing.append("analyst_estimates")
    elif isinstance(est, Exception):
        raise est
    elif est is None:
        missing.append("analyst_estimates")
        warnings.append("No analyst estimates published (little or no broker coverage).")

    orders = capex = None
    if isinstance(fil, (NSEError, ToolError)):
        warnings.append(f"Exchange filings unavailable: {fil}")
        missing.append("filings")
    elif isinstance(fil, Exception):
        raise fil
    else:
        orders, capex = fil

    if isinstance(com, ImportError):
        com = {"available": False,
               "note": "Install the AI extra (pip install 'screener-mcp[ai]') to extract management guidance "
                       "from earnings call transcripts."}
    elif isinstance(com, Exception):
        warnings.append(f"Earnings call analysis failed ({type(com).__name__}: {com}).")
        com = None
        missing.append("management_commentary")

    order_total = round(sum(o["amount_cr"] for o in orders if o["amount_cr"]), 2) if orders else None
    return ToolResult(
        data={
            "symbol": page.symbol,
            "name": ov.get("name"),
            "current_price": price,
            "analyst_estimates": est,
            "order_wins": {
                "window_days": days,
                "count": len(orders),
                "disclosed_value_cr": order_total or None,
                "items": orders[:25],
            } if orders is not None else None,
            "capex_and_expansion": {"count": len(capex), "items": capex[:15]} if capex is not None else None,
            "management_commentary": com,
            "notes": [
                "Order wins and capex items are matched by keywords in NSE filing headlines — open the "
                "filing to confirm scope, value and timing. disclosed_value_cr only sums amounts stated "
                "in headlines.",
                "Analyst estimates are consensus figures and change often; they are not guidance.",
            ],
        },
        warnings=warnings,
        missing_fields=missing,
        reason="Some forward-looking sources were unavailable." if missing else None,
        meta=page.meta,
    )
