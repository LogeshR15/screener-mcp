"""
Industry context from Screener.in's public industry pages.

Every company page links its classification (Broad Sector → Sector → Broad
Industry → Industry). Each industry page lists *every* listed company in it —
same table format as a screen, 25 rows a page, sortable — which is enough for:

  * industry medians (P/E, ROCE) → peer-relative valuation at scale, since
    stocks in the same industry share one cached fetch;
  * revenue share, rank and concentration (HHI, CR4) → market-position
    signals for moat analysis.

The peers AJAX endpoint only returns ~7 peers, too few for either.
"""

import asyncio
import re
import statistics
import time
from typing import Optional

from bs4 import BeautifulSoup

from ..client import get_client
from ..parsers.screener import parse_screen_results
from .numbers import to_number

_LEVELS = ("Broad Sector", "Sector", "Broad Industry", "Industry")
_CACHE_TTL = 30 * 60
_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}

# Rows below this market cap (₹ Cr) are left out of medians: micro-caps with
# stale or one-off numbers otherwise drag the "typical" P/E around.
MEDIAN_MIN_MCAP = 500


def parse_classification(html: str) -> dict[str, dict]:
    """{"Industry": {"name": ..., "url": "/market/..."}, "Sector": {...}, ...}"""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.select('a[href*="/market/"]'):
        level = a.get("title", "")
        if level in _LEVELS and level not in out:
            out[level] = {"name": re.sub(r"\s+", " ", a.get_text()).strip(), "url": a["href"]}
    return out


def _row(r: dict) -> dict:
    m = re.search(r"/company/((?:id/)?[^/]+)/", r.get("_url", ""))
    return {
        "symbol": m.group(1).upper() if m else None,
        "name": r.get("Company") or r.get("Name"),
        "company_id": r.get("_company_id"),
        "pe": to_number(r.get("Price to Earning")),
        "market_cap": to_number(r.get("Market Capitalization")),
        "roce": to_number(r.get("Return on capital employed")),
        "sales_qtr": to_number(r.get("Sales latest quarter")),
        "sales_growth_yoy": to_number(r.get("YOY Quarterly sales growth")),
        "dividend_yield": to_number(r.get("Dividend yield")),
    }


async def fetch_industry(url: str, max_pages: int = 6, by: str = "sales") -> dict:
    """Companies in an industry, from Screener's industry page.

    by="sales": sorted by quarterly sales and fetched up to ``max_pages`` —
        the pages we fetch hold nearly all of the industry's revenue, so shares
        and HHI are accurate (the tail beyond barely moves them).
    by="mcap": sorted by market cap, stopping once a page's smallest company
        falls below MEDIAN_MIN_MCAP — exactly the companies the medians use,
        usually a single page. Used for peer-relative valuation.
    """
    key = (url, by)

    def cached():
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            res = hit[1]
            if by == "mcap" or res["pages_fetched"] >= min(max_pages, res["total_pages"]):
                return res
        return None

    if (hit := cached()) is not None:
        return hit
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if (hit := cached()) is not None:
            return hit
        client = await get_client()
        sort = "sales latest quarter" if by == "sales" else "market capitalization"

        async def page(n: int) -> dict:
            params = {"sort": sort, "order": "desc"}
            if n > 1:
                params["page"] = str(n)
            return parse_screen_results(await client.get_html(url, params=params))

        first = await page(1)
        total_pages = first.get("total_pages") or 1
        pages = [first]
        if by == "sales":
            if total_pages > 1:
                pages += await asyncio.gather(*[page(n) for n in range(2, min(total_pages, max_pages) + 1)])
        else:
            while len(pages) < min(total_pages, max_pages):
                last = pages[-1].get("companies") or []
                smallest = to_number(last[-1].get("Market Capitalization")) if last else None
                if smallest is None or smallest < MEDIAN_MIN_MCAP:
                    break
                pages.append(await page(len(pages) + 1))
        rows, seen = [], set()
        for p in pages:
            for r in p.get("companies", []):
                row = _row(r)
                if row["company_id"] and row["company_id"] not in seen:
                    seen.add(row["company_id"])
                    rows.append(row)
        result = {
            "url": url,
            "sorted_by": by,
            "total_pages": total_pages,
            "pages_fetched": len(pages),
            "total_companies": first.get("total_results") or len(rows),
            "fetched_companies": len(rows),
            "rows": rows,
        }
        _cache[key] = (time.monotonic(), result)
        return result


def _median(values: list[float]) -> Optional[float]:
    return round(statistics.median(values), 2) if values else None


def industry_stats(industry: dict) -> dict:
    rows = industry["rows"]
    sizable = [r for r in rows if (r["market_cap"] or 0) >= MEDIAN_MIN_MCAP]
    pes = [r["pe"] for r in sizable if r["pe"] and 0 < r["pe"] < 500]
    roces = [r["roce"] for r in sizable if r["roce"] is not None]

    sales = [(r, r["sales_qtr"]) for r in rows if r["sales_qtr"] and r["sales_qtr"] > 0]
    total = sum(s for _, s in sales)
    shares = sorted(((r, s / total * 100) for r, s in sales), key=lambda x: -x[1]) if total else []
    hhi = round(sum(sh ** 2 for _, sh in shares)) if shares else None
    return {
        "companies": industry["total_companies"],
        "companies_in_stats": len(rows),
        "median_pe": _median(pes),
        "median_roce": _median(roces),
        "median_basis": f"companies with market cap ≥ ₹{MEDIAN_MIN_MCAP} Cr and positive P/E ({len(pes)} for P/E)",
        "total_sales_qtr_cr": round(total, 2) if total else None,
        "hhi": hhi,
        "concentration": (None if hhi is None else "highly concentrated" if hhi > 2500
                          else "moderately concentrated" if hhi > 1500 else "fragmented"),
        "cr4_pct": round(sum(sh for _, sh in shares[:4]), 2) if shares else None,
        "_shares": shares,
    }


def position_in(stats: dict, company_id: Optional[str]) -> Optional[dict]:
    for rank, (row, share) in enumerate(stats["_shares"], 1):
        if row["company_id"] == company_id:
            return {"revenue_share_pct": round(share, 2), "revenue_rank": rank,
                    "of_companies_with_sales": len(stats["_shares"])}
    return None


def public_stats(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if not k.startswith("_")}
