"""
Market context that Screener.in doesn't carry: recent news and analyst targets.

  get_recent_news     — headlines from Google News' public RSS search
  get_analyst_targets — Yahoo Finance consensus (mean/median/high/low target,
                        analyst count, rating split) plus broker target prices
                        mentioned in recent headlines

Both are third-party aggregations, labelled as such in every response, and
never mixed into Screener's own data.
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus

import httpx

from ..core.company_page import fetch_company_page
from ..core.envelope import ToolError, ToolResult
from ..core.numbers import to_number
from ..core.yahoo import YahooError, get_yahoo_client, raw
from ..parsers.company import parse_overview

_NEWS_URL = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
_SUFFIX_RE = re.compile(r"\s*\b(ltd\.?|limited|\(merged\))\s*$", re.I)

# "target price of ₹370", "TP ₹424", "target Rs 1,250", "₹370 target"
_TARGET_RES = [
    re.compile(r"(?:target(?:\s+price)?|\bTP\b)[^₹\d]{0,30}?(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)", re.I),
    re.compile(r"(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)\s*(?:price\s+)?(?:target|\bTP\b)", re.I),
]


def _short_name(name: str) -> str:
    return _SUFFIX_RE.sub("", name or "").strip()


def _news_query(name: str, nse_code: Optional[str], extra: str = "") -> str:
    parts = [f'"{_short_name(name)}"']
    if nse_code and len(nse_code) >= 4 and nse_code.isalpha():
        parts.append(f'"{nse_code}"')
    q = " OR ".join(parts)
    return f"({q}) {extra}".strip() if extra else q


async def fetch_news(query: str, days: int) -> list[dict]:
    """Google News RSS search → [{published, source, title, url}], newest first."""
    url = _NEWS_URL.format(q=quote_plus(f"{query} when:{days}d"))
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (httpx.HTTPError, ET.ParseError) as e:
        raise ToolError(f"Google News search failed ({type(e).__name__}) — this is a feed failure, "
                        "not an absence of news.", "upstream_unavailable", source="google_news")

    items, seen = [], set()
    for it in root.iter("item"):
        source = (it.findtext("source") or "").strip()
        title = (it.findtext("title") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if not title or key in seen:
            continue
        seen.add(key)
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            published = None
        items.append({
            "published": published.isoformat(timespec="minutes") if published else None,
            "source": source or None,
            "title": title,
            "url": it.findtext("link"),
        })
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return items


async def get_recent_news(symbol: str, days: int = 14, limit: int = 20) -> ToolResult:
    days = max(1, min(int(days or 14), 90))
    limit = max(1, min(int(limit or 20), 50))
    page = await fetch_company_page(symbol, "standalone")
    ov = parse_overview(page.html)
    query = _news_query(ov.get("name") or page.symbol, ov.get("nse_code"))
    items = await fetch_news(query, days)

    warnings = list(page.warnings)
    if not items:
        warnings.append(f"No news headlines found in the last {days} days (the feed responded normally).")
    return ToolResult(
        data={
            "symbol": page.symbol,
            "name": ov.get("name"),
            "days": days,
            "query": query,
            "count": len(items),
            "articles": items[:limit],
            "source_note": ("Headlines aggregated by Google News from third-party publishers — verify "
                            "important claims at the source. For official company filings use "
                            "get_company_announcements."),
        },
        warnings=warnings,
        meta=page.meta,
    )


def _extract_targets(title: str) -> list[float]:
    out = []
    for rx in _TARGET_RES:
        for m in rx.finditer(title):
            v = to_number(m.group(1))
            if v and v not in out:
                out.append(v)
    return out


async def _target_mentions(name: str, nse_code: Optional[str], price: Optional[float], days: int = 60) -> list[dict]:
    items = await fetch_news(_news_query(name, nse_code, "target price"), days)
    mentions = []
    for it in items:
        targets = _extract_targets(it["title"])
        if price:
            # drop numbers that can't be a target for this stock (years, lakh-crore figures…)
            targets = [t for t in targets if 0.3 * price <= t <= 4 * price]
        if targets:
            mentions.append({**it, "targets_inr": targets})
    return mentions


async def get_analyst_targets(symbol: str) -> ToolResult:
    page = await fetch_company_page(symbol, "standalone")
    ov = parse_overview(page.html)
    nse, bse = (ov.get("nse_code") or "").upper(), (ov.get("bse_code") or "")
    if not nse and not bse:
        raise ToolError(f"No NSE/BSE listing code found for {page.symbol}.", "no_data")
    ticker = f"{nse}.NS" if nse else f"{bse}.BO"
    price = to_number(ov.get("current_price"))

    async def consensus():
        summary = await get_yahoo_client().quote_summary(ticker, ["financialData", "recommendationTrend"])
        if not summary:
            return None
        fd = summary.get("financialData") or {}
        n = raw(fd, "numberOfAnalystOpinions")
        mean = raw(fd, "targetMeanPrice")
        if not n or not mean:
            return None
        ref = raw(fd, "currentPrice") or price
        trend = next((t for t in (summary.get("recommendationTrend") or {}).get("trend", [])
                      if t.get("period") == "0m"), None)
        return {
            "analysts": int(n),
            "target_mean": round(mean, 2),
            "target_median": raw(fd, "targetMedianPrice"),
            "target_high": raw(fd, "targetHighPrice"),
            "target_low": raw(fd, "targetLowPrice"),
            "reference_price": ref,
            "implied_upside_pct": round((mean / ref - 1) * 100, 2) if ref else None,
            "recommendation": (raw(fd, "recommendationKey") or None) if raw(fd, "recommendationKey") != "none" else None,
            "rating_split": {k: trend.get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")} if trend else None,
            "source": f"Yahoo Finance consensus for {ticker} (aggregated broker estimates)",
        }

    cons_res, mention_res = await asyncio.gather(
        consensus(), _target_mentions(ov.get("name") or page.symbol, nse or None, price), return_exceptions=True,
    )

    warnings, missing = list(page.warnings), []
    if isinstance(cons_res, YahooError):
        warnings.append(f"Consensus unavailable: {cons_res}.")
        missing.append("consensus")
        cons_res = None
    elif isinstance(cons_res, Exception):
        raise cons_res
    elif cons_res is None:
        missing.append("consensus")
        warnings.append(f"No analyst consensus published for {ticker} (likely little or no broker coverage).")
    if isinstance(mention_res, ToolError):
        warnings.append(f"Headline target search unavailable: {mention_res.message}")
        mention_res = None
        missing.append("headline_targets")
    elif isinstance(mention_res, Exception):
        raise mention_res

    if isinstance(cons_res, type(None)) and mention_res is None and "consensus" in missing \
            and any("unavailable" in w for w in warnings):
        raise ToolError("Neither the consensus feed nor the news search responded.", "upstream_unavailable")

    mentions = mention_res or []
    all_mentioned = [t for m in mentions for t in m["targets_inr"]]
    return ToolResult(
        data={
            "symbol": page.symbol,
            "name": ov.get("name"),
            "current_price": price,
            "consensus": cons_res,
            "headline_target_mentions": mentions[:15],
            "headline_target_summary": {
                "count": len(all_mentioned),
                "min": min(all_mentioned) if all_mentioned else None,
                "max": max(all_mentioned) if all_mentioned else None,
                "window_days": 60,
            },
            "notes": [
                "Consensus and headline targets come from different broker sets and dates, so they "
                "won't match exactly — neither is the market's single view.",
                "Headline targets are extracted from news titles automatically; open the article to "
                "confirm the broker, date and rating.",
            ],
        },
        warnings=warnings,
        missing_fields=missing,
        reason=("Some target sources were unavailable." if missing else None),
        meta=page.meta,
    )
