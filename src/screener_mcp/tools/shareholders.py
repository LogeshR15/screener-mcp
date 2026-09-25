"""
Shareholder search — find bulk deal activity by investor/entity name via NSE.
"""

import logging
from datetime import datetime, timedelta

from ..core.company_page import resolve_nse_symbol
from ..core.envelope import ToolError, ToolResult
from ..core.nse_client import get_nse_client

logger = logging.getLogger(__name__)


def _deal_row(d: dict, with_symbol: bool) -> dict:
    row = {
        "date": d.get("date") or None,
        "buy_sell": d.get("buySell") or None,
        "quantity": d.get("quantityTraded") or None,
        "price": d.get("tradePrice") or None,
        "client": d.get("clientName") or None,
    }
    if with_symbol:
        row = {"symbol": d.get("symbol") or None, **row}
    return row


def _coverage(days: int, days_queried: int, failed: list[str]) -> tuple[list[str], bool, str | None]:
    warnings, partial, reason = [], False, None
    if days > days_queried:
        warnings.append(
            f"NSE's bulk-deals endpoint serves one day per request, so only the most recent "
            f"{days_queried} of the requested {days} days were searched."
        )
    if failed:
        partial = True
        reason = (f"NSE requests failed for {len(failed)} of {days_queried} days "
                  f"({', '.join(failed[:5])}{' …' if len(failed) > 5 else ''}) — deals on those days are missing.")
    return warnings, partial, reason


async def search_shareholder(
    name: str,
    symbol: str = None,
    days: int = 365,
) -> ToolResult:
    """
    Search NSE bulk/block deals for a shareholder name.

    Note: Only captures NSE bulk deals (single trade > 0.5% of equity).
    """
    if not name.strip():
        raise ToolError("Please provide a shareholder name to search.", "invalid_input")

    warnings, meta = [], {}
    nse_symbol = None
    if symbol:
        nse_symbol, warnings, meta = await resolve_nse_symbol(symbol)

    nse = await get_nse_client()
    to_date = datetime.now().strftime("%d-%m-%Y")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")
    deals, failed, days_queried = await nse.get_bulk_deals(from_date, to_date, symbol=nse_symbol)

    name_lower = name.lower()
    matched = [d for d in deals if name_lower in str(d.get("clientName", "")).lower()]
    cov_warnings, partial, reason = _coverage(days, days_queried, failed)
    warnings += cov_warnings
    if not matched:
        warnings.append(
            f"No bulk deals by '{name}' among {len(deals)} deals searched. Bulk deals only cover single "
            "trades > 0.5% of equity — use get_shareholding_pattern for gradual accumulation."
        )
    return ToolResult(
        data={
            "name": name,
            "symbol": nse_symbol,
            "period": {"from": from_date, "to": to_date, "days_searched": days_queried},
            "deals_searched": len(deals),
            "matches": len(matched),
            "deals": [_deal_row(d, with_symbol=True) for d in matched[:100]],
        },
        warnings=warnings,
        partial=partial,
        reason=reason,
        meta=meta,
    )


async def get_bulk_deals(symbol: str, days: int = 90) -> ToolResult:
    """
    All NSE bulk deals for one company — no investor name required.

    Note: Only captures NSE bulk deals (single trade > 0.5% of equity).
    """
    nse_symbol, warnings, meta = await resolve_nse_symbol(symbol)
    nse = await get_nse_client()
    to_date = datetime.now().strftime("%d-%m-%Y")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")
    deals, failed, days_queried = await nse.get_bulk_deals(from_date, to_date, symbol=nse_symbol)

    cov_warnings, partial, reason = _coverage(days, days_queried, failed)
    warnings += cov_warnings
    if not deals:
        warnings.append(
            f"No bulk deals for {nse_symbol} in the days searched. That's common — bulk deals "
            "(>0.5% of equity in one trade) are rare. Use get_shareholding_pattern for FII/DII/promoter trends."
        )
    return ToolResult(
        data={
            "symbol": nse_symbol,
            "period": {"from": from_date, "to": to_date, "days_searched": days_queried},
            "count": len(deals),
            "deals": [_deal_row(d, with_symbol=False) for d in deals[:100]],
        },
        warnings=warnings,
        partial=partial,
        reason=reason,
        meta=meta,
    )
