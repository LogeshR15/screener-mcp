"""
Year-end valuation multiples (P/E, P/B) from Screener.in's chart API.

Screener's "Ratios" table on a company page only carries working-capital days
and ROCE — no valuation multiples. The chart API behind the P/E and P/B charts
has a daily series going back ~10 years:

  GET /api/company/{company_id}/chart/?q=Price to Earning-Median PE-EPS&days=3650
  GET /api/company/{company_id}/chart/?q=Price to book value-Median PBV-Book value&days=3650

so a fiscal year's multiple is the last point on or before its year-end date.
"""

import asyncio
import calendar
import time
from datetime import date, datetime
from typing import Optional

from ..client import get_client

_QUERIES = {
    "pe": ("Price to Earning-Median PE-EPS", "Price to Earning"),
    "pb": ("Price to book value-Median PBV-Book value", "Price to book value"),
}
_DAYS = 3650
_CACHE_TTL = 6 * 3600
_cache: dict[tuple[str, bool], tuple[float, dict]] = {}


def fiscal_year_end(label: str) -> Optional[date]:
    """'Mar 2024' → 2024-03-31. None for 'TTM' or anything unparseable."""
    try:
        d = datetime.strptime(label.strip(), "%b %Y")
    except (ValueError, AttributeError):
        return None
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _series(payload: dict, metric: str) -> list[tuple[str, float]]:
    for ds in (payload or {}).get("datasets", []):
        if ds.get("metric") == metric:
            out = []
            for point in ds.get("values", []):
                try:
                    out.append((point[0], float(point[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            return sorted(out)
    return []


async def fetch_multiples(company_id: str, consolidated: bool) -> dict[str, list[tuple[str, float]]]:
    """{"pe": [(iso date, value), ...], "pb": [...]} — daily, oldest first."""
    key = (company_id, consolidated)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    client = await get_client()

    async def one(q: str) -> dict:
        params = {"q": q, "days": str(_DAYS)}
        if consolidated:
            params["consolidated"] = "true"
        payload = await client.get_json(f"/api/company/{company_id}/chart/", params=params)
        return payload if isinstance(payload, dict) else {}

    payloads = await asyncio.gather(*[one(q) for q, _ in _QUERIES.values()])
    result = {k: _series(p, metric) for (k, (_, metric)), p in zip(_QUERIES.items(), payloads)}
    _cache[key] = (time.monotonic(), result)
    return result


def value_at_year_end(series: list[tuple[str, float]], label: str) -> Optional[float]:
    """Last value on or before the fiscal year-end; None if the series starts
    after it (so years older than the chart window stay blank, not guessed)."""
    end = fiscal_year_end(label)
    if not end or not series:
        return None
    cutoff = end.isoformat()
    if series[0][0] > cutoff:
        return None
    best = None
    for d, v in series:
        if d > cutoff:
            break
        best = v
    return best
