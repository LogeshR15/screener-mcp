"""
Shareholder search — find bulk deal activity by investor/entity name via NSE.
"""

import logging
from datetime import datetime, timedelta

from ..core.nse_client import get_nse_client

logger = logging.getLogger(__name__)


async def search_shareholder(
    name: str,
    symbol: str = None,
    days: int = 365,
) -> str:
    """
    Search NSE bulk/block deals for a shareholder name.

    name: partial or full investor/entity name (e.g., "Jhunjhunwala", "SBI Mutual Fund")
    symbol: optional NSE symbol to narrow search to one company
    days: how many days of history to search (default 365)

    Note: Only captures NSE bulk deals (single trade > 0.5% of equity).
    Regular FII/DII/promoter accumulation below that threshold won't appear here.
    """
    if not name.strip():
        return "**Error:** Please provide a shareholder name to search."

    nse = await get_nse_client()

    to_date = datetime.now().strftime("%d-%m-%Y")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")

    deals = await nse.get_bulk_deals(from_date, to_date, symbol=symbol)

    if not deals:
        return (
            f"**No bulk deal data returned from NSE.**\n\n"
            f"Date range: {from_date} to {to_date}\n"
            + (f"Symbol filter: {symbol.upper()}\n" if symbol else "")
            + "\nNSE may be temporarily unavailable, or no bulk deals exist in this period."
        )

    name_lower = name.lower()

    def _name_fields(d: dict) -> str:
        return " ".join([
            str(d.get("clientName", "")),
            str(d.get("client_name", "")),
            str(d.get("buyerSellName", "")),
            str(d.get("client", "")),
        ]).lower()

    matched = [d for d in deals if name_lower in _name_fields(d)]

    if not matched:
        return (
            f"**No bulk deals found for '{name}'** in the last {days} days.\n\n"
            f"Total bulk deals searched: {len(deals)}\n"
            + (f"Symbol filter: {symbol.upper()}\n" if symbol else "")
            + "\n**What this covers:** NSE bulk deals only (single trade > 0.5% of company equity).\n"
            + "Smaller accumulation/disposition doesn't appear here.\n\n"
            + "**Alternatives:**\n"
            + "  - Use `get_shareholding_pattern(symbol)` to see quarterly FII/DII/Promoter trends\n"
            + "  - Check Screener.in's 'Shareholders' tab for top individual holders"
        )

    lines = [
        f"# Bulk Deals — '{name}'",
        f"Period: {from_date} to {to_date} | {len(matched)} deals found"
        + (f" | Symbol: {symbol.upper()}" if symbol else ""),
        "",
        f"{'Date':<12} {'Company':<20} {'B/S':<5} {'Qty (shares)':<15} {'Price ₹':<10} Client",
        "-" * 85,
    ]

    for d in matched[:40]:
        date = str(d.get("tradDt", d.get("date", "")))[:10]
        company = str(d.get("symbol", d.get("scripCode", "")))[:19]
        bs = str(d.get("buySell", d.get("buy_sell", "?")))[:4]
        qty = str(d.get("quantityTraded", d.get("qty", "")))
        price = str(d.get("tradePrice", d.get("price", "")))
        client = str(
            d.get("clientName") or d.get("client_name") or d.get("buyerSellName") or ""
        )[:35]
        lines.append(f"{date:<12} {company:<20} {bs:<5} {qty:<15} {price:<10} {client}")

    if len(matched) > 40:
        lines.append(f"\n... and {len(matched) - 40} more deals. Use `symbol` param to narrow.")

    lines.append(
        "\n**Note:** NSE bulk deals (>0.5% of equity in a single trade) only. "
        "For full shareholding, use `get_shareholding_pattern(symbol)`."
    )
    return "\n".join(lines)


async def get_bulk_deals(symbol: str, days: int = 90) -> str:
    """
    All NSE bulk deals for one company — no investor name required.

    symbol: NSE trading symbol (e.g., "RELIANCE")
    days: how many days of history to search (default 90)

    Note: Only captures NSE bulk deals (single trade > 0.5% of equity).
    """
    if not symbol.strip():
        return "**Error:** Please provide an NSE symbol."

    nse = await get_nse_client()

    to_date = datetime.now().strftime("%d-%m-%Y")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")

    deals = await nse.get_bulk_deals(from_date, to_date, symbol=symbol)

    if not deals:
        return (
            f"**No bulk deals found for {symbol.upper()}** in the last {days} days.\n\n"
            f"Date range: {from_date} to {to_date}\n\n"
            "This is common — bulk deals (>0.5% of equity in a single trade) are relatively rare "
            "events. For ongoing FII/DII/Promoter trends, use `get_shareholding_pattern(symbol)`."
        )

    lines = [
        f"# Bulk Deals — {symbol.upper()}",
        f"Period: {from_date} to {to_date} | {len(deals)} deals found",
        "",
        f"{'Date':<12} {'B/S':<5} {'Qty (shares)':<15} {'Price ₹':<10} Client",
        "-" * 75,
    ]

    for d in deals[:50]:
        date = str(d.get("tradDt", d.get("date", "")))[:10]
        bs = str(d.get("buySell", d.get("buy_sell", "?")))[:4]
        qty = str(d.get("quantityTraded", d.get("qty", "")))
        price = str(d.get("tradePrice", d.get("price", "")))
        client = str(
            d.get("clientName") or d.get("client_name") or d.get("buyerSellName") or ""
        )[:40]
        lines.append(f"{date:<12} {bs:<5} {qty:<15} {price:<10} {client}")

    if len(deals) > 50:
        lines.append(f"\n... and {len(deals) - 50} more deals. Narrow with a smaller `days` value.")

    lines.append(
        "\n**Note:** NSE bulk deals (>0.5% of equity in a single trade) only. "
        "For a specific investor across companies, use `search_shareholder(name)`."
    )
    return "\n".join(lines)
