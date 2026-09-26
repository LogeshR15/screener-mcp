"""
Company announcements — fetch and filter NSE corporate disclosures.
"""

import logging
from datetime import datetime

from ..core.company_page import resolve_nse_symbol
from ..core.envelope import ToolError, ToolResult
from ..core.nse_client import get_nse_client

logger = logging.getLogger(__name__)

# Checked in order. Rating categories come first: their filings often mention
# NCDs or results ("rating for NCDs"), which would otherwise win. NSE files ESG
# scores under "Credit Rating" too, so ESG is split out before credit ratings.
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "esg_rating": ["esg score", "esg rating", "esg risk", "esg ratings"],
    "credit_rating": ["credit rating", "rating action", "crisil", "icra", "care ratings", "india ratings",
                      "rating agency", "acuite", "brickwork", "infomerics"],
    "results": ["financial results", "quarterly results", "annual results", "q1", "q2", "q3", "q4", "half year"],
    "board_meeting": ["board meeting", "board of directors"],
    "dividend": ["dividend"],
    "insider_trading": ["insider trading", "promoter", "bulk deal", "block deal", "sast"],
    "agm": ["agm", "annual general meeting", "egm", "extraordinary general meeting"],
    "acquisition": ["acquisition", "merger", "demerger", "amalgamation", "takeover"],
    "buyback": ["buyback", "buy-back", "share repurchase"],
    "fund_raise": ["rights issue", "ipo", "fpo", "ncd", "debenture", "preferential allotment"],
}

# Rating actions are infrequent (often 1-2 a year), so a 30-day default window
# usually finds nothing — these categories default to two years.
_DEFAULT_DAYS = {"credit_rating": 730, "esg_rating": 730}


def _categorize(headline: str, subject: str) -> str:
    text = (headline + " " + subject).lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return "general"


def _within_days(date_str: str, days: int) -> bool:
    """Return True if date_str falls within the last N days."""
    for fmt in ["%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y"]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return (datetime.now() - dt).days <= days
        except ValueError:
            continue
    return True  # include if unparseable


async def _fetch_filtered(symbol: str, category: str, days: int) -> tuple[str, list[dict], int, int, list[str], dict]:
    valid_categories = {"all"} | set(_CATEGORY_KEYWORDS.keys())
    if category not in valid_categories:
        raise ToolError(
            f"Invalid category '{category}'. Valid options: {', '.join(sorted(valid_categories))}",
            "invalid_input",
        )
    days = max(1, min(int(days or _DEFAULT_DAYS.get(category, 30)), 3650))
    nse_symbol, warnings, meta = await resolve_nse_symbol(symbol)
    nse = await get_nse_client()
    items = await nse.get_announcements(nse_symbol)  # raises NSEError on failure

    filtered = [a for a in items if _within_days(a.get("date", ""), days)]
    if category != "all":
        filtered = [
            a for a in filtered
            if _categorize(a.get("headline", ""), a.get("category", "")) == category
        ]
    rows = [
        {
            "date": a.get("date") or None,
            "category": _categorize(a.get("headline", ""), a.get("category", "")),
            "nse_category": a.get("category") or None,
            "headline": (a.get("headline") or a.get("category") or "")[:300] or None,
            "url": a.get("url") or None,
        }
        for a in filtered
    ]
    return nse_symbol, rows, len(items), days, warnings, meta


async def get_company_announcements(
    symbol: str,
    category: str = "all",
    days: int = 0,
) -> ToolResult:
    """
    Fetch recent company announcements from NSE.

    symbol: NSE trading symbol or company name (resolved via Screener.in)
    category: "all" | "results" | "board_meeting" | "dividend" | "insider_trading"
              | "agm" | "acquisition" | "buyback" | "fund_raise" | "credit_rating" | "esg_rating"
    days: look back this many days (0 = default: 730 for rating categories, else 30)
    """
    nse_symbol, rows, total, days, warnings, meta = await _fetch_filtered(symbol, category, days)
    if total == 0:
        warnings.append(
            f"NSE returned no announcements at all for {nse_symbol}. The request succeeded, "
            "so this is NSE's answer rather than a failure — but it's unusual for a listed company."
        )
    elif not rows and category == "credit_rating":
        warnings.append(
            f"No credit rating actions for {nse_symbol} in the last {days} days ({total} announcements "
            "fetched). Often this means the company has no rated debt (common for low-debt businesses)."
        )
    elif not rows:
        warnings.append(
            f"No {category} announcements in the last {days} days ({total} announcements "
            "across all categories/dates). Try category='all' or a larger `days`."
        )
    limit = 50
    if len(rows) > limit:
        warnings.append(f"Showing the latest {limit} of {len(rows)}. Narrow with `category` or `days`.")
    return ToolResult(
        data={
            "symbol": nse_symbol,
            "category": category,
            "days": days,
            "total_announcements_fetched": total,
            "matches": len(rows),
            "announcements": rows[:limit],
        },
        warnings=warnings,
        meta=meta,
    )
