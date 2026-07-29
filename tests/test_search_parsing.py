"""Tests for parsing /api/company/search/ responses."""

import pytest

from screener_mcp.parsers.screener import _extract_id, parse_search_results


@pytest.mark.parametrize(
    "url,expected",
    [
        # Plain form, no financial-type suffix.
        ("/company/TCS/", "TCS"),
        # The common form - most companies come back with the suffix.
        ("/company/AEQUS/consolidated/", "AEQUS"),
        ("/company/INFY/standalone/", "INFY"),
        # Companies with no NSE ticker are keyed by BSE code.
        ("/company/531569/consolidated/", "531569"),
        # Companies with neither use the internal-id form. The symbol is the
        # segment after "id", not the literal "id".
        ("/company/id/246883/consolidated/", "246883"),
        ("/company/id/246883/", "246883"),
        # Degenerate input should not raise.
        ("", ""),
    ],
)
def test_extract_id(url, expected):
    assert _extract_id(url) == expected


def test_search_results_drop_full_text_search_row():
    """
    The search API appends a "Search everywhere: <query>" row with a null id
    pointing at /full-text-search/. It is not a company and must not be
    returned as one.
    """
    payload = [
        {"id": 1285800, "name": "Aequs Ltd", "url": "/company/AEQUS/consolidated/"},
        {"id": None, "name": "Search everywhere: Aequs", "url": "/full-text-search/?q=Aequs"},
    ]
    results = parse_search_results(payload)
    assert results == [
        {
            "name": "Aequs Ltd",
            "url": "/company/AEQUS/consolidated/",
            "screener_id": "AEQUS",
        }
    ]


def test_search_results_empty():
    assert parse_search_results([]) == []
