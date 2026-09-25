"""Parsing helpers for Screener-formatted numbers."""

import re
from typing import Optional

_NON_NUMERIC = re.compile(r"[^0-9.\-]")


def to_number(text) -> Optional[float]:
    """Parse '₹ 1,23,456 Cr.', '23.4%', '-278' or 42 into a float.

    Returns None — never 0 — for blank / unparseable input, so a missing value
    can't be mistaken for a genuine zero.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = _NON_NUMERIC.sub("", str(text))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def blank_to_none(value):
    """Map '' / whitespace-only strings to None; pass everything else through."""
    if isinstance(value, str) and not value.strip():
        return None
    return value
