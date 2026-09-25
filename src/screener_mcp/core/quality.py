"""
Data-quality checks: missing-field detection and sanity bounds on ratios.

Two failure modes these guard against:

  * Silent blanks — a field the source page had no value for used to come
    through as "" next to valid-looking data. ``overview_missing_fields`` names
    them so the tool can report ``status: partial``.

  * Parse artifacts presented as data — e.g. a Cash Conversion Cycle of -278
    days next to Days Payable of 345. ``check_ratio_history`` flags values
    outside plausible ranges (and internally inconsistent CCC rows) with
    ``data_quality_flag: true``. Flagged values are kept, not dropped — some
    extremes are genuine (real-estate inventory days run to years) — but they
    are never passed through unmarked.
"""

import re
from typing import Any, Optional

from .numbers import to_number

# Overview fields a caller relies on, mapped to where parse_overview puts them.
# key_ratios labels vary slightly across Screener layouts, so each maps to a
# list of accepted labels.
OVERVIEW_CORE_FIELDS: dict[str, tuple[str, ...]] = {
    "current_price": ("current_price",),
    "52_week_high": ("52_week_high",),
    "52_week_low": ("52_week_low",),
    "market_cap": ("Market Cap",),
    "pe": ("Stock P/E",),
    "book_value": ("Book Value",),
    "dividend_yield": ("Dividend Yield",),
    "roce": ("ROCE", "Return on capital employed"),
    "roe": ("ROE", "Return on equity"),
}


def overview_field(overview: dict, field: str):
    """Read a core field from a parse_overview() result, '' → None."""
    for key in OVERVIEW_CORE_FIELDS.get(field, (field,)):
        if key in overview:
            value = overview.get(key)
        else:
            value = overview.get("key_ratios", {}).get(key)
        if value is not None and str(value).strip():
            return value
    return None


def overview_missing_fields(overview: dict) -> list[str]:
    return [f for f in OVERVIEW_CORE_FIELDS if overview_field(overview, f) is None]


def explain_missing(missing: list[str], all_core_missing: bool) -> str:
    if all_core_missing:
        return (
            "Source page returned no data — Screener.in rendered the company page "
            "with every price and ratio field empty."
        )
    reason = "Screener.in's page had no value for these fields."
    if "pe" in missing:
        reason += " (Stock P/E is left blank for loss-making companies.)"
    if "dividend_yield" in missing:
        reason += " (Dividend yield is blank when no dividend was paid.)"
    return reason


# ─── ratio-history sanity bounds ──────────────────────────────────────────────

# (min, max) inclusive plausible range per ratios-table row. Values outside get
# flagged. Ranges are deliberately generous: the goal is catching parse
# artifacts, not second-guessing unusual-but-real businesses.
RATIO_BOUNDS: dict[str, tuple[float, float]] = {
    "debtor days": (0, 365),
    "inventory days": (0, 730),
    "days payable": (0, 365),
    "cash conversion cycle": (-180, 730),
    # customer advances (defence, EPC) legitimately push this far negative
    "working capital days": (-1000, 1000),
    "roce %": (-100, 200),
    "roe %": (-200, 300),
}

_CCC_TOLERANCE_DAYS = 5


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[+\-]$", "", label or "").strip()).lower()


def _flag(period: str, value: float, raw: str, reason: str) -> dict:
    return {
        "period": period,
        "value": value,
        "raw": raw,
        "data_quality_flag": True,
        "reason": reason,
    }


def check_ratio_history(years: list[str], rows: list[dict]) -> dict[str, list[dict]]:
    """Return {row label: [flag, ...]} for implausible ratio-history values.

    Checks each row against RATIO_BOUNDS, and each period's Cash Conversion
    Cycle against Debtor Days + Inventory Days − Days Payable (the identity
    Screener computes it from — a mismatch means the columns were misparsed).
    """
    flags: dict[str, list[dict]] = {}
    by_label: dict[str, dict] = {_norm_label(r.get("label", "")): r for r in rows}

    def period_values(row: dict) -> list[tuple[str, Optional[float], str]]:
        values = row.get("values", [])
        # values align to the right-most years when a row is shorter
        offset = len(years) - len(values)
        out = []
        for i, raw in enumerate(values):
            period = years[i + offset] if 0 <= i + offset < len(years) else f"col{i}"
            out.append((period, to_number(raw), raw))
        return out

    for key, (lo, hi) in RATIO_BOUNDS.items():
        row = by_label.get(key)
        if not row:
            continue
        for period, value, raw in period_values(row):
            if value is None:
                continue
            if value < lo or value > hi:
                reason = f"outside plausible range [{lo:g}, {hi:g}]"
                if key == "inventory days" and value > hi:
                    reason += " — genuine for some long-cycle businesses (e.g. real estate), otherwise a likely parse error"
                else:
                    reason += " — likely a filing/parse artifact; verify against the filing before use"
                flags.setdefault(row["label"], []).append(_flag(period, value, raw, reason))

    parts = [by_label.get(k) for k in ("debtor days", "inventory days", "days payable", "cash conversion cycle")]
    if all(parts):
        debtor, inventory, payable, ccc = (dict((p, v) for p, v, _ in period_values(r)) for r in parts)
        raw_ccc = {p: raw for p, _, raw in period_values(parts[3])}
        for period, ccc_val in ccc.items():
            d, i, p = debtor.get(period), inventory.get(period), payable.get(period)
            if None in (ccc_val, d, i, p):
                continue
            expected = d + i - p
            if abs(ccc_val - expected) > _CCC_TOLERANCE_DAYS:
                already = any(f["period"] == period for f in flags.get(parts[3]["label"], []))
                if not already:
                    flags.setdefault(parts[3]["label"], []).append(_flag(
                        period, ccc_val, raw_ccc.get(period, ""),
                        f"inconsistent with Debtor + Inventory − Payable days ({expected:g}) — likely misparsed",
                    ))

    return flags


def annotate_rows(years: list[str], rows: list[dict], flags: dict[str, list[dict]]) -> list[dict[str, Any]]:
    """Attach flags to their rows: row gets data_quality_flag + flags list."""
    out = []
    for row in rows:
        entry = {"label": row.get("label", ""), "values": row.get("values", [])}
        row_flags = flags.get(row.get("label", ""))
        if row_flags:
            entry["data_quality_flag"] = True
            entry["flags"] = row_flags
        out.append(entry)
    return out
