"""
Insider trading disclosures — SEBI PIT Regulation 7(2) filings via NSE.

Distinct from bulk deals (get_bulk_deals): insider
disclosures capture every trade by a promoter, KMP, or designated person,
no matter how small — bulk deals only catch single trades over 0.5% of
a company's equity, which misses most of the gradual buying or selling
that actually signals insider sentiment.
"""

import asyncio
import logging
from datetime import datetime

from ..core.company_page import resolve_nse_symbol
from ..core.envelope import ToolResult
from ..core.nse_client import get_nse_client

logger = logging.getLogger(__name__)


def _fmt_date(raw: str) -> str:
    """NSE broadcast timestamps look like '22-May-2026 22:07:04' — trim to the date."""
    return raw.split(" ")[0] if raw else ""


def _num(v):
    return int(v) if str(v or "").isdigit() else None


def _structured_row(t: dict) -> dict:
    """A `/api/corporates-pit` trade (fields inline) in the tool's row shape."""
    return {
        "date": _fmt_date(t.get("date", "")) or None,
        "person": t.get("acqName") or None,
        "person_category": t.get("personCategory") or None,
        "transaction": (t.get("tdpTransactionType") or "").upper() or None,
        "shares": _num(t.get("secAcq")),
        "value_inr": _num(t.get("secVal")),
        "mode": t.get("acqMode") or None,
        "holding_pre_pct": t.get("befAcqSharesPer") or None,
        "holding_post_pct": t.get("afterAcqSharesPer") or None,
        "note": (t.get("remarks") if t.get("remarks") not in (None, "-") else None),
    }


def _sort_key(row: dict):
    try:
        return datetime.strptime(row.get("date") or "", "%d-%b-%Y")
    except ValueError:
        return datetime.min


async def get_insider_trading(symbol: str, days: int = 365) -> ToolResult:
    """
    Recent insider trading disclosures (SEBI PIT Regulation 7(2)) for a company.

    symbol: NSE trading symbol or company name (resolved via Screener.in)
    days: look-back window for the structured trade feed (default 365)

    Shows who traded (promoter/KMP/designated person), buy or sell,
    quantity, value, and their holding before/after — a signal bulk
    deals miss because it has no minimum trade-size threshold.

    Merges NSE's two PIT feeds: `corporates-pit` (trade fields inline) and
    `corporates-pit-gg` (newer XBRL filings). Either alone misses filings.
    """
    days = max(30, min(int(days or 365), 1825))
    nse_symbol, warnings, meta = await resolve_nse_symbol(symbol)
    nse = await get_nse_client()
    # raises NSEError if both fail; one failing makes the result partial
    filings, trades = await asyncio.gather(
        nse.get_insider_trading(nse_symbol), nse.get_insider_trades(nse_symbol, days), return_exceptions=True)
    failed_feeds = [name for name, r in (("corporates-pit-gg", filings), ("corporates-pit", trades))
                    if isinstance(r, Exception)]
    if len(failed_feeds) == 2:
        raise filings
    filings = [] if isinstance(filings, Exception) else filings
    trades = [] if isinstance(trades, Exception) else trades

    rows, incomplete = [], 0
    for f in filings:
        if f.get("_detail_error"):
            incomplete += 1
        value = f.get("tradeValue")
        rows.append({
            "date": _fmt_date(f.get("broadcastDateTime", "")) or None,
            "person": f.get("personName") or None,
            "person_category": f.get("personCategory") or None,
            "transaction": (f.get("transactionType") or "").upper() or None,
            "shares": _num(f.get("securitiesTraded")),
            "value_inr": _num(value),
            "mode": f.get("modeOfAcquisition") or None,
            "holding_pre_pct": f.get("holdingPrePct") or None,
            "holding_post_pct": f.get("holdingPostPct") or None,
            "note": f.get("revisionRemark") or None,
            **({"details_unavailable": True} if f.get("_detail_error") else {}),
        })

    seen = {(r["person"] or "").lower() + str(r["shares"]) for r in rows if r["person"]}
    for t in trades:
        row = _structured_row(t)
        key = (row["person"] or "").lower() + str(row["shares"])
        if row["person"] and key in seen:
            continue
        seen.add(key)
        rows.append(row)
    rows.sort(key=_sort_key, reverse=True)

    if not rows and not failed_feeds:
        warnings.append(
            f"NSE reports no insider-trading (PIT) disclosures for {nse_symbol} in the last {days} days "
            "on either PIT feed. Both requests succeeded, so this is a real empty result."
        )
    reasons = []
    if failed_feeds:
        reasons.append(f"NSE feed {failed_feeds[0]} failed — disclosures listed only on it are missing.")
    if incomplete:
        reasons.append(f"Trade details (person, quantity, value) couldn't be fetched for {incomplete} of "
                       f"{len(rows)} filing(s) — those rows are marked details_unavailable.")
    reason = " ".join(reasons) or None
    return ToolResult(
        data={
            "symbol": nse_symbol,
            "days": days,
            "filings": rows,
            "note": ("Covers every disclosed promoter/KMP/designated-person trade regardless of size. "
                     "For large third-party block trades use get_bulk_deals; for aggregate holdings "
                     "use get_shareholding_pattern."),
        },
        warnings=warnings,
        partial=bool(incomplete or failed_feeds),
        reason=reason,
        meta=meta,
    )
