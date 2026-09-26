"""
Year-by-year series pulled out of Screener's statement tables, plus the
ratios Screener's own "Ratios" table leaves out (ROE, debt-to-equity).

Tables are aligned by column label, not position: the balance sheet can carry
an extra half-year column ("Sep 2025") that the P&L doesn't, and the P&L has a
TTM column that isn't a fiscal year.
"""

from typing import Optional

from ..parsers.company import split_ttm
from .numbers import to_number


def series(table: dict, label_prefix: str) -> dict[str, Optional[float]]:
    """{column label: value} for the first row whose label starts with
    ``label_prefix`` (case-insensitive). Empty if there's no such row."""
    years, rows, _ = split_ttm(table)
    prefix = label_prefix.lower()
    for r in rows:
        if r.get("label", "").lower().startswith(prefix):
            return {y: to_number(v) for y, v in zip(years, r.get("values", []))}
    return {}


def fiscal_years(table: dict) -> list[str]:
    return split_ttm(table)[0]


def equity(balance_sheet: dict) -> dict[str, Optional[float]]:
    cap, res = series(balance_sheet, "equity capital"), series(balance_sheet, "reserves")
    out = {}
    for y in cap:
        if cap.get(y) is None and res.get(y) is None:
            out[y] = None
        else:
            out[y] = (cap.get(y) or 0) + (res.get(y) or 0)
    return out


def roe_by_year(profit_loss: dict, balance_sheet: dict) -> dict[str, Optional[float]]:
    """Net profit ÷ average shareholders' equity (opening and closing), in %.

    The first year has no opening balance, so it uses closing equity. Years
    with non-positive equity are left blank — the ratio means nothing there.
    """
    profit = series(profit_loss, "net profit")
    eq = equity(balance_sheet)
    years = [y for y in fiscal_years(profit_loss) if y in eq]
    out: dict[str, Optional[float]] = {}
    prev = None
    for y in years:
        close = eq.get(y)
        base = (prev + close) / 2 if prev and close else close
        p = profit.get(y)
        out[y] = round(p / base * 100, 1) if p is not None and base and base > 0 else None
        prev = close
    return out


def debt_to_equity_by_year(balance_sheet: dict) -> dict[str, Optional[float]]:
    borrowings, eq = series(balance_sheet, "borrowings"), equity(balance_sheet)
    return {
        y: (round(borrowings[y] / eq[y], 2) if borrowings.get(y) is not None and eq.get(y) and eq[y] > 0 else None)
        for y in borrowings
    }


def cagr(values: dict[str, Optional[float]], years: int) -> Optional[float]:
    """CAGR in % over the last ``years`` intervals of a {label: value} series.
    None if either end is missing or non-positive (CAGR is undefined there)."""
    vals = list(values.values())
    if len(vals) < years + 1:
        return None
    start, end = vals[-years - 1], vals[-1]
    if not start or not end or start <= 0 or end <= 0:
        return None
    return round(((end / start) ** (1 / years) - 1) * 100, 1)


def last(values: dict[str, Optional[float]], n: int = 1) -> list[Optional[float]]:
    return list(values.values())[-n:]
