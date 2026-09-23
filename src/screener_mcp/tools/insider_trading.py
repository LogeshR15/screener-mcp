"""
Insider trading disclosures — SEBI PIT Regulation 7(2) filings via NSE.

Distinct from bulk deals (search_shareholder/get_bulk_deals): insider
disclosures capture every trade by a promoter, KMP, or designated person,
no matter how small — bulk deals only catch single trades over 0.5% of
a company's equity, which misses most of the gradual buying or selling
that actually signals insider sentiment.
"""

import logging

from ..core.nse_client import get_nse_client

logger = logging.getLogger(__name__)


def _fmt_date(raw: str) -> str:
    """NSE broadcast timestamps look like '22-May-2026 22:07:04' — trim to the date."""
    return raw.split(" ")[0] if raw else ""


async def get_insider_trading(symbol: str) -> str:
    """
    Recent insider trading disclosures (SEBI PIT Regulation 7(2)) for a company.

    symbol: NSE trading symbol (e.g., "RELIANCE", "INFY")

    Shows who traded (promoter/KMP/designated person), buy or sell,
    quantity, value, and their holding before/after — a signal bulk
    deals miss because it has no minimum trade-size threshold.
    """
    if not symbol.strip():
        return "**Error:** Please provide an NSE symbol."

    nse = await get_nse_client()
    filings = await nse.get_insider_trading(symbol)

    if not filings:
        return (
            f"**No insider trading disclosures found for {symbol.upper()}.**\n\n"
            "Possible reasons:\n"
            f"  - Symbol is incorrect — use `search_company('{symbol}')` to verify\n"
            "  - NSE API is temporarily unavailable\n"
            "  - No promoter/KMP/designated-person trades reported recently\n"
        )

    lines = [
        f"# Insider Trading — {symbol.upper()}",
        f"SEBI PIT Regulation 7(2) disclosures | {len(filings)} filing(s)",
        "",
    ]

    for f in filings:
        date = _fmt_date(f.get("broadcastDateTime", ""))
        person = f.get("personName") or "Unknown"
        category = f.get("personCategory") or "—"
        txn = (f.get("transactionType") or "—").upper()
        qty = f.get("securitiesTraded") or "—"
        value = f.get("tradeValue") or ""
        value_fmt = f"₹{int(value):,}" if str(value).isdigit() else "—"
        mode = f.get("modeOfAcquisition") or "—"
        pre = f.get("holdingPrePct") or ""
        post = f.get("holdingPostPct") or ""

        lines.append(f"## {date} — {person} ({category})")
        lines.append(f"  {txn} {qty} shares | Value: {value_fmt} | Mode: {mode}")
        if pre or post:
            lines.append(f"  Holding: {pre or '?'}% -> {post or '?'}%")
        if f.get("revisionRemark"):
            lines.append(f"  Note: {f['revisionRemark']}")
        lines.append("")

    lines.append(
        "**Note:** Covers every disclosed promoter/KMP/designated-person trade, "
        "regardless of size. For large third-party block trades, use "
        "`get_bulk_deals(symbol)` or `search_shareholder(name)` instead. "
        "For aggregate FII/DII/Promoter %, use `get_shareholding_pattern(symbol)`."
    )
    return "\n".join(lines)
