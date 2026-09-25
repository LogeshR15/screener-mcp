"""
Local portfolio tracking — holdings, average cost, and live P&L.

Stored as a single JSON file at ~/.screener-mcp/portfolio.json. No account or
network service required; this is purely local state for the long-term
investor use case (Tapetide gates the equivalent behind a paid account).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from ..core.company_page import fetch_company_page
from ..core.envelope import ToolError, ToolResult
from ..parsers.company import parse_overview

logger = logging.getLogger(__name__)

_PORTFOLIO_PATH = Path.home() / ".screener-mcp" / "portfolio.json"


def _load() -> dict:
    if not _PORTFOLIO_PATH.exists():
        return {"holdings": {}}
    try:
        data = json.loads(_PORTFOLIO_PATH.read_text())
        data.setdefault("holdings", {})
        return data
    except Exception:
        return {"holdings": {}}


def _save(data: dict):
    _PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PORTFOLIO_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _parse_num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip())
    except (ValueError, TypeError):
        return None


async def _live_price(symbol: str) -> Optional[float]:
    try:
        page = await fetch_company_page(symbol, "consolidated")
        overview = parse_overview(page.html)
        return _parse_num(overview.get("current_price"))
    except Exception as e:
        logger.warning(f"Failed to fetch live price for {symbol}: {e}")
        return None


async def add_portfolio_stock(symbol: str, quantity: float, avg_price: float) -> str:
    """
    Add a holding to your local portfolio, or merge into an existing one.

    If the symbol is already held, the new lot is merged into the existing
    position using a quantity-weighted average price (like a real broker
    ledger — buying more shares at a different price updates your average
    cost, it doesn't overwrite it).

    symbol: NSE/BSE symbol (e.g., "RELIANCE")
    quantity: number of shares in this lot
    avg_price: price per share for this lot
    """
    symbol = symbol.upper().strip()
    if quantity <= 0 or avg_price <= 0:
        raise ToolError("`quantity` and `avg_price` must both be positive.", "invalid_input")

    data = _load()
    holdings = data["holdings"]

    if symbol in holdings:
        existing = holdings[symbol]
        old_qty = existing["quantity"]
        old_avg = existing["avg_price"]
        new_qty = old_qty + quantity
        new_avg = ((old_qty * old_avg) + (quantity * avg_price)) / new_qty
        holdings[symbol] = {"quantity": new_qty, "avg_price": round(new_avg, 4)}
        _save(data)
        return (
            f"**Merged into existing {symbol} position.**\n\n"
            f"Previous: {old_qty} shares @ ₹{old_avg}\n"
            f"Added: {quantity} shares @ ₹{avg_price}\n"
            f"New position: {new_qty} shares @ avg ₹{round(new_avg, 2)}"
        )

    holdings[symbol] = {"quantity": quantity, "avg_price": avg_price}
    _save(data)
    return f"**Added {symbol}:** {quantity} shares @ ₹{avg_price} (invested: ₹{round(quantity * avg_price, 2)})"


async def update_portfolio_stock(
    symbol: str,
    quantity: Optional[float] = None,
    avg_price: Optional[float] = None,
) -> str:
    """
    Overwrite quantity and/or average price for an existing holding —
    e.g. after a partial sell (set the new remaining quantity) or a
    correction to your recorded cost basis.

    Use `add_portfolio_stock` instead if you're adding a new buy lot and want
    the average price recalculated automatically.
    """
    symbol = symbol.upper().strip()
    data = _load()
    holdings = data["holdings"]

    if symbol not in holdings:
        raise ToolError(f"{symbol} is not in your portfolio. Use add_portfolio_stock to add it first.", "not_found")

    if quantity is None and avg_price is None:
        raise ToolError("Provide at least one of `quantity` or `avg_price` to update.", "invalid_input")

    if quantity is not None:
        if quantity <= 0:
            raise ToolError("`quantity` must be positive. Use `remove_portfolio_stock` to exit a position entirely.", "invalid_input")
        holdings[symbol]["quantity"] = quantity
    if avg_price is not None:
        if avg_price <= 0:
            raise ToolError("`avg_price` must be positive.", "invalid_input")
        holdings[symbol]["avg_price"] = avg_price

    _save(data)
    h = holdings[symbol]
    return f"**Updated {symbol}:** {h['quantity']} shares @ avg ₹{h['avg_price']}"


async def remove_portfolio_stock(symbol: str) -> str:
    """Remove a holding entirely from the portfolio (full exit)."""
    symbol = symbol.upper().strip()
    data = _load()
    if symbol not in data["holdings"]:
        raise ToolError(f"{symbol} is not in your portfolio.", "not_found")
    del data["holdings"][symbol]
    _save(data)
    return f"**Removed {symbol} from portfolio.**"


async def get_portfolio() -> ToolResult:
    """
    View your portfolio with live prices, P&L, and per-holding weight.

    Fetches the current price for each holding from Screener.in and computes
    invested value, current value, absolute/percentage gain, and each
    position's weight in the total portfolio.
    """
    data = _load()
    holdings = data["holdings"]

    if not holdings:
        return (
            "**Portfolio is empty.**\n\n"
            "Add a holding with: `add_portfolio_stock('RELIANCE', quantity=10, avg_price=1350)`"
        )

    rows = []
    total_invested = 0.0
    total_current = 0.0
    price_errors = []

    prices = await asyncio.gather(*[_live_price(sym) for sym in holdings])
    for (symbol, h), price in zip(holdings.items(), prices):
        qty = h["quantity"]
        avg = h["avg_price"]
        invested = qty * avg

        if price is None:
            price_errors.append(symbol)
            rows.append({
                "symbol": symbol, "qty": qty, "avg": avg, "price": None,
                "invested": invested, "current": None, "pnl": None, "pnl_pct": None,
            })
            total_invested += invested
            continue

        current = qty * price
        pnl = current - invested
        pnl_pct = (pnl / invested * 100) if invested else 0.0
        rows.append({
            "symbol": symbol, "qty": qty, "avg": avg, "price": price,
            "invested": invested, "current": current, "pnl": pnl, "pnl_pct": pnl_pct,
        })
        total_invested += invested
        total_current += current

    holdings_out = []
    for r in sorted(rows, key=lambda x: x["current"] or 0, reverse=True):
        weight = (r["current"] / total_current * 100) if r["current"] and total_current else None
        holdings_out.append({
            "symbol": r["symbol"],
            "quantity": r["qty"],
            "avg_price": round(r["avg"], 2),
            "ltp": r["price"],
            "invested": round(r["invested"], 2),
            "current_value": round(r["current"], 2) if r["current"] is not None else None,
            "pnl": round(r["pnl"], 2) if r["pnl"] is not None else None,
            "pnl_pct": round(r["pnl_pct"], 2) if r["pnl_pct"] is not None else None,
            "weight_pct": round(weight, 2) if weight is not None else None,
        })

    priced_invested = sum(r["invested"] for r in rows if r["current"] is not None)
    totals = {
        "invested": round(total_invested, 2),
        "current_value": round(total_current, 2) if total_current else None,
        # P&L only over holdings with a live price, so a missing price can't masquerade as a loss
        "pnl": round(total_current - priced_invested, 2) if total_current else None,
        "pnl_pct": round((total_current - priced_invested) / priced_invested * 100, 2) if total_current and priced_invested else None,
        "pnl_basis": "holdings with a live price only" if price_errors else "all holdings",
    }
    return ToolResult(
        data={"holdings": holdings_out, "totals": totals, "currency": "INR"},
        missing_fields=[f"{sym}.price" for sym in price_errors],
        reason=(f"Live price unavailable for {', '.join(price_errors)} — totals.pnl covers only holdings "
                "with a live price.") if price_errors else None,
    )
