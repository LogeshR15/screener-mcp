"""
Offline tests for the tool registry and the docs that describe it.

These make no network calls and need no Screener.in credentials, so they run
in CI. They exist mainly to stop the docs from drifting away from the code:
if you add, remove, or rename a tool, these fail until README.md and the
server module docstring are updated to match.
"""

import re
from pathlib import Path

import pytest

from screener_mcp import server as server_module
from screener_mcp.server import mcp
from screener_mcp.tools.screening_tools import THEMES, parse_filters

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# The full public tool surface. Update this list deliberately when you add a
# tool — that is the point of the test, not an obstacle to it.
EXPECTED_TOOLS = {
    # Company research
    "search_company",
    "get_company_overview",
    "get_financials",
    "get_quarterly_results",
    "get_shareholding_pattern",
    "get_peer_comparison",
    "compare_companies",
    "get_full_analysis",
    "analyze_red_flags",
    # Stock screening
    "screen_stocks",
    "get_52_week_low_candidates",
    "compare_to_sector",
    "get_recent_news",
    "get_analyst_targets",
    "get_relative_valuation",
    "get_moat_signals",
    "get_forward_outlook",
    "screen_by_theme",
    # Document analysis (RAG)
    "get_document_list",
    "ask_company_research",
    "search_market_commentary",
    # Corporate events
    "get_company_announcements",
    "get_bulk_deals",
    "get_insider_trading",
    # Market & research
    "get_commodity_prices",
    "notebook_ai",
    # Portfolio
    "add_portfolio_stock",
    "update_portfolio_stock",
    "remove_portfolio_stock",
    "get_portfolio",
}


@pytest.fixture(scope="module")
async def tool_names():
    return {t.name for t in await mcp.list_tools()}


async def test_registered_tools_match_expected(tool_names):
    """The registry matches EXPECTED_TOOLS exactly — no surprise additions or drops."""
    assert tool_names == EXPECTED_TOOLS, (
        f"added: {sorted(tool_names - EXPECTED_TOOLS)}, "
        f"removed: {sorted(EXPECTED_TOOLS - tool_names)}"
    )


async def test_every_tool_has_a_description(tool_names):
    """A tool with no description is invisible to the model that has to pick it."""
    missing = [t.name for t in await mcp.list_tools() if not (t.description or "").strip()]
    assert not missing, f"tools with no description: {missing}"


async def test_readme_tool_count_is_accurate(tool_names):
    """README's 'Tools — N total' heading matches the real count."""
    heading = re.search(r"^## Tools — (\d+) total", README.read_text(), re.MULTILINE)
    assert heading, "README is missing the '## Tools — N total' heading"
    assert int(heading.group(1)) == len(tool_names)


async def test_every_tool_is_documented_in_readme(tool_names):
    readme = README.read_text()
    undocumented = [name for name in tool_names if f"`{name}`" not in readme]
    assert not undocumented, f"tools missing from README: {undocumented}"


async def test_every_tool_is_listed_in_module_docstring(tool_names):
    """server.py's docstring is the first thing a contributor reads — keep it true."""
    docstring = server_module.__doc__ or ""
    missing = [name for name in tool_names if name not in docstring]
    assert not missing, f"tools missing from server.py docstring: {missing}"


def test_every_theme_is_well_formed():
    """Each theme has a description and exactly one of: a Screener query, or a
    sector universe plus filters that parse."""
    for key, t in THEMES.items():
        assert t.get("description"), key
        assert ("query" in t) != ("universe" in t), f"{key}: needs exactly one of query / universe"
        if "universe" in t:
            assert t["universe"] and parse_filters(t["filters"]), key


async def test_every_theme_and_its_criteria_are_in_the_tool_description():
    """list_investment_themes was folded into screen_by_theme — its description
    must carry every theme's criteria."""
    desc = next(t.description for t in await mcp.list_tools() if t.name == "screen_by_theme")
    for key, t in THEMES.items():
        assert key in desc and (t.get("query") or t["filters"]) in desc, key


def test_every_theme_is_documented_in_readme():
    readme = README.read_text()
    undocumented = [theme for theme in THEMES if theme not in readme]
    assert not undocumented, f"themes missing from README: {undocumented}"


def test_core_import_needs_no_optional_ai_dependencies():
    """
    The server must import with only the core deps installed; pdfplumber,
    chromadb and sentence-transformers are optional extras and the
    document-analysis tools raise a clear ImportError at call time instead.
    """
    import importlib

    importlib.reload(server_module)  # would raise here if an extra leaked into import
    assert server_module.mcp is not None
