"""
Offline regressions for the 0.5.0 tester-feedback pass: statement cleanup,
computed ratios, shareholding / pledge, red flags, the merged tools, sector
themes, NSE categorisation, news relevance and the document pipeline.

tests/fixtures/itc_company_page.html is a trimmed real Screener page (ITC,
consolidated, Sep 2026) — ITC has no promoter group, a TTM column, a "Raw PDF"
quarterly row and "+" row labels, which is what these tests need.
"""

from pathlib import Path

import pytest

from screener_mcp import server
from screener_mcp.core import company_page as cp
from screener_mcp.core import history as hist
from screener_mcp.core import rag
from screener_mcp.core.valuation_history import fiscal_year_end, value_at_year_end
from screener_mcp.parsers.company import (
    parse_balance_sheet, parse_profit_loss, parse_quarterly_results, parse_shareholding, pledged_pct, split_ttm,
)
from screener_mcp.tools import analysis_tools as at
from screener_mcp.tools import company_tools as ct
from screener_mcp.tools import documents as docs
from screener_mcp.tools import insider_trading as it_mod
from screener_mcp.tools import market_tools as mt
from screener_mcp.tools import screening_tools as st
from screener_mcp.tools import shareholders as sh_mod
from screener_mcp.tools.announcements import _categorize

ITC = (Path(__file__).parent / "fixtures" / "itc_company_page.html").read_text()


@pytest.fixture
def pages(monkeypatch):
    """Serve pages from a dict keyed by (symbol, financial_type)."""
    served = {}

    async def fake_get_page(symbol, financial_type):
        return served.get((symbol, financial_type))

    async def no_freshness(company_id):
        return {}

    async def fake_multiples(company_id, consolidated):
        return {"pe": [("2024-03-28", 24.6), ("2025-03-28", 26.1), ("2026-03-31", 17.9)],
                "pb": [("2024-03-28", 7.2), ("2025-03-28", 6.8), ("2026-03-31", 5.2)]}

    async def no_peers(page):
        return {"symbol": page.symbol, "columns": [], "rows": []}, ["peers"], "no peers offline"

    monkeypatch.setattr(cp, "_get_page", fake_get_page)
    monkeypatch.setattr(ct, "price_freshness", no_freshness)
    monkeypatch.setattr(ct, "fetch_multiples", fake_multiples)
    monkeypatch.setattr(at, "peers_data", no_peers)
    return served


# ─── statement cleanup ────────────────────────────────────────────────────────

def test_row_labels_lose_the_plus_suffix_and_raw_pdf_row_is_dropped():
    labels = [r["label"] for r in parse_profit_loss(ITC)["rows"]]
    assert "Sales" in labels and "Net Profit" in labels and not any(l.endswith("+") for l in labels)
    assert "Raw PDF" not in [r["label"] for r in parse_quarterly_results(ITC)["rows"]]
    assert not any(r["category"].endswith("+") for r in parse_shareholding(ITC)["rows"])


def test_ttm_is_split_from_fiscal_years():
    years, rows, ttm = split_ttm(parse_profit_loss(ITC))
    assert "TTM" not in years and years[-1] == "Mar 2026"
    assert len(rows[0]["values"]) == len(years) and ttm["Sales"]


async def test_years_counts_fiscal_years_and_ttm_is_separate(pages):
    pages[("ITC", "consolidated")] = ITC
    env = await server.get_financials("ITC", "profit_loss", years=3)
    assert env["data"]["years"] == ["Mar 2024", "Mar 2025", "Mar 2026"]
    assert all(len(r["values"]) == 3 for r in env["data"]["rows"])
    assert isinstance(env["data"]["ttm"]["Sales"], float)


async def test_ratios_include_computed_roe_debt_and_valuation_history(pages):
    pages[("ITC", "consolidated")] = ITC
    env = await server.get_financials("ITC", "ratios", years=3)
    rows = {r["label"]: r["values"] for r in env["data"]["rows"]}
    assert {"ROE %", "Debt to equity", "P/E (year-end)", "P/B (year-end)"} <= set(rows)
    assert rows["P/E (year-end)"] == [24.6, 26.1, 17.9]
    assert 25 < rows["ROE %"][-1] < 35                      # Screener's own latest ROE is ~29
    assert env["data"]["row_sources"]["ROE %"].startswith("computed")


def test_roe_uses_average_equity():
    pl = {"years": ["Mar 2025", "Mar 2026"], "rows": [{"label": "Net Profit", "values": ["10", "30"]}]}
    bs = {"years": ["Mar 2025", "Mar 2026"], "rows": [
        {"label": "Equity Capital", "values": ["10", "10"]}, {"label": "Reserves", "values": ["90", "190"]}]}
    assert hist.roe_by_year(pl, bs) == {"Mar 2025": 10.0, "Mar 2026": 20.0}   # 30 / avg(100, 200)


def test_year_end_multiple_is_last_point_on_or_before_year_end():
    series = [("2024-03-27", 20.0), ("2024-03-28", 21.0), ("2024-04-01", 25.0)]
    assert fiscal_year_end("Mar 2024").isoformat() == "2024-03-31"
    assert value_at_year_end(series, "Mar 2024") == 21.0
    assert value_at_year_end(series, "Mar 2023") is None      # before the chart window: blank, not guessed
    assert value_at_year_end(series, "TTM") is None


# ─── shareholding / pledge ────────────────────────────────────────────────────

async def test_no_promoter_group_is_detected_and_pledge_not_applicable(pages):
    pages[("ITC", "standalone")] = ITC
    env = await server.get_shareholding_pattern("ITC")
    d = env["data"]
    assert d["promoter_group"]["present"] is False
    assert d["pledge"]["status"] == "not_applicable"
    assert d["trends"]["FIIs"]["direction"] in {"increasing", "decreasing", "stable"}


def test_pledge_is_read_from_screener_cons_not_the_shareholding_table():
    page = ('<section id="analysis"><div class="cons"><ul>'
            '<li>Promoter holding is low: 29.7%</li><li>Promoters have pledged 89.4% of their holding.</li>'
            '</ul></div></section>')
    assert pledged_pct(page) == 89.4
    assert pledged_pct(ITC) is None
    assert ct._pledge(page, has_promoter=True)["severity"] == "high"
    assert ct._pledge(ITC, has_promoter=True)["status"] == "not_flagged"


# ─── red flags / full analysis ────────────────────────────────────────────────

async def test_red_flags_are_computed_with_evidence(pages):
    pages[("ITC", "consolidated")] = ITC
    env = await server.analyze_red_flags("ITC")
    d = env["data"]
    checks = {f["check"] for f in d["flags"]}
    assert "other_income_history" in checks or "other_income" in checks   # FY25 demerger gain
    assert all(f["severity"] in {"high", "medium", "low"} and f["evidence"] for f in d["flags"])
    assert any(s["check"] == "promoter_holding" for s in d["skipped_checks"])
    assert "ANALYST TASK" not in str(d)


async def test_full_analysis_windows_match_standalone_tools(pages):
    pages[("ITC", "consolidated")] = ITC
    env = await server.get_full_analysis("ITC")
    report = env["data"]["report"]
    sh = report[report.index("## Shareholding"):].split("\n")[2]
    assert len(sh.split()) - 1 == 8 * 2                     # 8 quarters ("Sep 2024" is two tokens)
    pl_header = report[report.index("## Profit & Loss"):].split("\n")[2]
    assert pl_header.split()[-1] == "TTM" and pl_header.count("Mar ") == 10
    assert "loads via AJAX" not in report


# ─── merged tools ─────────────────────────────────────────────────────────────

async def test_compare_companies_feeds_dashboard_and_json(pages):
    pages[("ITC", "consolidated")] = ITC
    pages[("ITC2", "consolidated")] = ITC
    env = await server.compare_companies("ITC,ITC2")
    assert env["status"] == "ok" and env["count"] == 2
    row = env["stocks"][0]
    assert row["pe_ratio"] == env["data"]["companies"][0]["pe"] and isinstance(row["debt_to_equity"], float)


async def test_bulk_deals_needs_symbol_or_name():
    env = await server.get_bulk_deals()
    assert env["status"] == "error" and env["error"]["type"] == "invalid_input"


async def test_bulk_deals_by_name_across_market(monkeypatch):
    rows = [{"symbol": "AJAX", "clientName": "SBI MUTUAL FUND", "buySell": "BUY"},
            {"symbol": "EPL", "clientName": "SOMEONE ELSE", "buySell": "SELL"}]

    class Fake:
        async def get_bulk_deals(self, from_date, to_date, symbol=None):
            return rows, [], 30

    async def fake_client():
        return Fake()

    monkeypatch.setattr(sh_mod, "get_nse_client", fake_client)
    env = await server.get_bulk_deals(name="mutual fund")
    assert env["data"]["count"] == 1 and env["data"]["deals"][0]["symbol"] == "AJAX"


def test_esg_scores_are_not_credit_ratings():
    assert _categorize("Intimation of ESG Score assigned by SES ESG Research", "Credit Rating- New") == "esg_rating"
    assert _categorize("ITC Limited has informed the Exchange about Credit Rating", "Credit Rating") == "credit_rating"
    assert _categorize("CRISIL reaffirms rating on NCDs", "Credit Rating") == "credit_rating"


async def test_insider_trading_merges_both_nse_feeds(monkeypatch):
    class Fake:
        async def get_insider_trading(self, symbol):
            return [{"broadcastDateTime": "22-May-2026 22:07:04", "personName": "A", "securitiesTraded": "10"}]

        async def get_insider_trades(self, symbol, days):
            return [{"date": "18-Feb-2026 19:06", "acqName": "B", "secAcq": "5", "tdpTransactionType": "Sell"},
                    {"date": "22-May-2026 20:00", "acqName": "A", "secAcq": "10"}]   # same trade as feed 1

    async def fake_client():
        return Fake()

    async def fake_resolve(symbol):
        return symbol, [], {"symbol": symbol}

    monkeypatch.setattr(it_mod, "get_nse_client", fake_client)
    monkeypatch.setattr(it_mod, "resolve_nse_symbol", fake_resolve)
    env = await server.get_insider_trading("RELIANCE")
    assert [r["person"] for r in env["data"]["filings"]] == ["A", "B"]
    assert env["data"]["filings"][1]["transaction"] == "SELL"


# ─── themes ───────────────────────────────────────────────────────────────────

def test_theme_filters_and_missing_fields():
    f = st.parse_filters("Market Capitalization > 500 AND Return on equity > 12")
    assert st._passes({"Market Capitalization": 900, "Return on equity": 15}, f) == (True, [])
    assert st._passes({"Market Capitalization": 900}, f) == (False, ["Return on equity"])


def test_rising_profit_falling_price_has_a_price_condition():
    assert "Return over 1year < 0" in st.THEMES["rising_profit_falling_price"]["query"]
    assert "Dividend preceding year" in st.THEMES["dividend_aristocrats"]["query"]


async def test_sector_theme_screens_its_universe(monkeypatch):
    def cand(sym, mcap, roe, growth=20):
        return {"symbol": sym, "name": sym, "company_id": sym,
                "fundamentals": {"Market Capitalization": mcap, "Return on equity": roe, "Profit growth 5Years": growth,
                                 "Price to Earning": 30}}

    async def fake_fetch(query=None, index_slug=None, max_rows=0, sort="", order="", page_path=None):
        if index_slug:
            return [cand("HAL", 300000, 24), cand("TINY", 100, 30)], 2
        return [cand("HAL", 300000, 24), cand("BEL", 280000, 27), cand("LOWROE", 5000, 5)], 3

    monkeypatch.setattr(st, "fetch_candidates", fake_fetch)
    env = await server.screen_by_theme("defence", limit=5)
    d = env["data"]
    assert d["theme"] == "defense" and [r["symbol"] for r in d["results"]] == ["HAL", "BEL"]
    assert d["universe_size"] == 4 and d["universe"][0]["type"] == "nse_index"


def test_dividend_yield_spike_is_flagged():
    assert any("dividend yield" in f for f in st.sanity_flags({"Dividend yield": 458, "Price to Earning": 0.14}))


# ─── news relevance ───────────────────────────────────────────────────────────

def test_code_that_is_a_word_of_the_name_is_not_searched():
    assert mt._news_query("Reliance Industries Ltd", "RELIANCE") == '"Reliance Industries"'
    assert '"TMPV"' in mt._news_query("Tata Motors Passenger Vehicles Ltd", "TMPV")
    assert '"ITC Ltd"' in mt._news_query("ITC Ltd", "ITC")          # acronym name → anchored phrases


@pytest.mark.parametrize("title,keep", [
    ("Khaitan & Co advises Reliance Industries on ₹12,000 crore NCD issuance", True),
    ("Reliance stock heads into the open after a 2.25 percent drop", True),
    ("India's urea self-reliance push draws 17 proposals worth Rs.2 trillion", False),
    ("AI reliance could erode independent thinking, warns Walsh", False),
])
def test_off_topic_headlines_are_dropped(title, keep):
    assert mt._mentions_company(title, "Reliance Industries Ltd", "RELIANCE") is keep


# ─── documents / RAG ──────────────────────────────────────────────────────────

def test_annual_reports_deduped_per_year_preferring_pdf():
    reports = [{"year": "2013", "url": "https://x/ar2013.zip"}, {"year": "2013", "url": "https://x/ar2013.pdf"},
               {"year": "2014", "url": "https://x/ar2014.zip"}]
    out = docs._dedupe_reports(reports)
    assert [(r["year"], r["format"]) for r in out] == [("2014", "zip"), ("2013", "pdf")]


def test_column_gutter_found_only_on_two_column_pages():
    two_col = [{"x0": x, "x1": x + 40} for x in (50, 100, 150, 200) for _ in range(10)] + \
              [{"x0": x, "x1": x + 40} for x in (330, 380, 430, 480) for _ in range(10)]
    gutter = rag.column_gutter(two_col, 600)
    assert gutter is not None and 240 < gutter < 330
    one_col = [{"x0": x, "x1": x + 60} for x in range(50, 540, 30) for _ in range(3)]
    assert rag.column_gutter(one_col, 600) is None


def test_mirrored_and_rotated_glyphs_are_dropped():
    assert rag._readable_char({"object_type": "char", "upright": True, "matrix": (1, 0, 0, 1, 0, 0)})
    assert not rag._readable_char({"object_type": "char", "upright": True, "matrix": (-1, 0, 0, 1, 0, 0)})
    assert not rag._readable_char({"object_type": "char", "upright": False, "matrix": (0, 1, -1, 0, 0, 0)})


def test_rerank_demotes_brsr_boilerplate_and_spreads_pages():
    q = "What are the key regulatory risks?"
    chunks = [
        {"text": "BRSR Principle 1 Essential Indicators risk", "score": 0.60, "metadata": {"page_start": 300}, "collection": "c"},
        {"text": "Regulatory risk: cigarette taxation and excise changes", "score": 0.55, "metadata": {"page_start": 40}, "collection": "c"},
        {"text": "More regulatory risks on taxation", "score": 0.54, "metadata": {"page_start": 41}, "collection": "c"},
        {"text": "Supply chain risks", "score": 0.50, "metadata": {"page_start": 90}, "collection": "c"},
    ]
    out = rag.rerank(chunks, q, top_k=3)
    assert out[0]["metadata"]["page_start"] == 40
    assert [c["metadata"]["page_start"] for c in out] == [40, 90, 300]     # page 41 dropped as near-duplicate


def test_excerpt_is_capped_around_the_question_terms():
    text = " ".join(["Filler sentence about nothing."] * 60 + ["Cigarette taxation is the key risk."] + ["More filler."] * 60)
    out = rag.excerpt(text, "cigarette taxation risk", max_chars=200)
    assert len(out) <= 204 and "Cigarette taxation" in out and out.startswith("…")
