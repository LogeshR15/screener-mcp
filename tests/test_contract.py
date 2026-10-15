"""Contract tests: recorded Screener pages vs. what the parsers and themes need.

Offline. When Screener changes a page, refresh the fixtures
(``python scripts/refresh_fixtures.py``), and these fail exactly where the
code no longer matches — before users see blank research data.
"""

import asyncio
from pathlib import Path

from screener_mcp.client import ScreenerClient
from screener_mcp.core import metrics
from screener_mcp.parsers.company import pledged_pct
from screener_mcp.parsers.screener import parse_screen_results
from screener_mcp.tools import screening_tools as st
from screener_mcp.tools.technical_tools import _row_to_candidate

FIXTURES = Path(__file__).parent / "fixtures"

# Columns available on company pages for curated lists (see _PAGE_FIELDS).
CURATED_FIELDS = set(st._PAGE_FIELDS.values())


def _industry_columns() -> set[str]:
    parsed = parse_screen_results((FIXTURES / "industry_page.html").read_text())
    cands = [c for c in map(_row_to_candidate, parsed["companies"]) if c]
    assert cands, "industry fixture parsed to no companies"
    return set().union(*(c["fundamentals"] for c in cands))


def test_sector_theme_filters_only_use_columns_the_pages_carry():
    industry = _industry_columns()
    for key, theme in st.THEMES.items():
        if "universe" not in theme:
            continue
        sources = {kind for kind, _ in theme["universe"]}
        fields = {f for f, _, _ in st.parse_filters(theme["filters"])}
        if "industry" in sources:
            assert fields <= industry, f"{key}: industry pages lack {fields - industry}"
        if "symbols" in sources:
            assert fields <= CURATED_FIELDS, f"{key}: company pages lack {fields - CURATED_FIELDS}"


def test_pledge_is_read_from_recorded_page():
    assert pledged_pct((FIXTURES / "pledged_company_page.html").read_text()) is not None


async def test_concurrent_identical_requests_share_one_http_call(monkeypatch):
    metrics.reset()
    client = ScreenerClient()
    calls = []

    async def fake_fetch(url, params):
        calls.append(url)
        await asyncio.sleep(0.05)
        return "<html></html>"

    monkeypatch.setattr(client, "_fetch_html", fake_fetch)
    results = await asyncio.gather(*[client.get_html("/company/TCS/") for _ in range(5)])
    assert results == ["<html></html>"] * 5 and len(calls) == 1
    assert metrics.snapshot()["coalesced_requests"] == 4
    await client.get_html("/company/TCS/")  # finished request isn't cached: fetches again
    assert len(calls) == 2


async def test_coalesced_failure_reaches_every_caller(monkeypatch):
    client = ScreenerClient()

    async def boom(url, params):
        await asyncio.sleep(0.01)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(client, "_fetch_html", boom)
    results = await asyncio.gather(*[client.get_html("/x/") for _ in range(3)], return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert not client._inflight


def test_metrics_snapshot_rates():
    metrics.reset()
    metrics.incr("page_cache_hits", 3)
    metrics.incr("page_cache_misses")
    metrics.incr("http_requests", 2)
    metrics.observe_latency(1.0)
    snap = metrics.snapshot()
    assert snap["page_cache_hit_rate"] == 0.75 and snap["avg_latency_ms"] == 500
