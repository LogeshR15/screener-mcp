"""
Insider trading disclosures — SEBI PIT Regulation 7(2) filings via NSE.

Distinct from bulk deals (search_shareholder/get_bulk_deals): insider
disclosures capture every trade by a promoter, KMP, or designated person,
no matter how small — bulk deals only catch single trades over 0.5% of
a company's equity, which misses most of the gradual buying or selling
that actually signals insider sentiment.
"""

import logging

from ..core.company_page import resolve_nse_symbol
from ..core.envelope import ToolResult
from ..core.nse_client import get_nse_client

logger = logging.getLogger(__name__)


def _fmt_date(raw: str) -> str:
    """NSE broadcast timestamps look like '22-May-2026 22:07:04' — trim to the date."""
    return raw.split(" ")[0] if raw else ""


async def get_insider_trading(symbol: str) -> ToolResult:
    """
    Recent insider trading disclosures (SEBI PIT Regulation 7(2)) for a company.

    symbol: NSE trading symbol or company name (resolved via Screener.in)

    Shows who traded (promoter/KMP/designated person), buy or sell,
    quantity, value, and their holding before/after — a signal bulk
    deals miss because it has no minimum trade-size threshold.
    """
    nse_symbol, warnings, meta = await resolve_nse_symbol(symbol)
    nse = await get_nse_client()
    filings = await nse.get_insider_trading(nse_symbol)  # raises NSEError on failure

    def num(v):
        return int(v) if str(v or "").isdigit() else None

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
            "shares": num(f.get("securitiesTraded")),
            "value_inr": num(value),
            "mode": f.get("modeOfAcquisition") or None,
            "holding_pre_pct": f.get("holdingPrePct") or None,
            "holding_post_pct": f.get("holdingPostPct") or None,
            "note": f.get("revisionRemark") or None,
            **({"details_unavailable": True} if f.get("_detail_error") else {}),
        })

    if not rows:
        warnings.append(
            f"NSE reports no insider-trading (PIT) disclosures for {nse_symbol} recently. "
            "The request succeeded, so this is a real empty result."
        )
    reason = None
    if incomplete:
        reason = (f"Trade details (person, quantity, value) couldn't be fetched for {incomplete} of "
                  f"{len(rows)} filing(s) — those rows are marked details_unavailable.")
    return ToolResult(
        data={
            "symbol": nse_symbol,
            "filings": rows,
            "note": ("Covers every disclosed promoter/KMP/designated-person trade regardless of size. "
                     "For large third-party block trades use get_bulk_deals; for aggregate holdings "
                     "use get_shareholding_pattern."),
        },
        warnings=warnings,
        partial=bool(incomplete),
        reason=reason,
        meta=meta,
    )
