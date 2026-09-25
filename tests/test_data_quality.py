"""
Offline tests for the response envelope, symbol resolution, consolidated →
standalone fallback, missing-field detection, ratio sanity checks, technical
screening, sector mapping and document-index freshness.

Network calls are replaced with monkeypatched fakes, so these run in CI.
"""

import pytest

from screener_mcp import server
from screener_mcp.core import company_page as cp
from screener_mcp.core import rag
from screener_mcp.core.company_page import Candidate, page_has_data, resolve_symbol, score_candidate
from screener_mcp.core.envelope import ToolError, ToolResult, to_envelope
from screener_mcp.core.indices import resolve_index, sector_index_for
from screener_mcp.core.numbers import to_number
from screener_mcp.core.quality import check_ratio_history, overview_missing_fields
from screener_mcp.core.technicals import PriceHistory, compute_technicals, rsi, sma
from screener_mcp.parsers.company import parse_overview
from screener_mcp.parsers.screener import parse_screen_results
from screener_mcp.tools import technical_tools as tt
from screener_mcp.tools.technical_tools import split_query

# ─── fixtures ─────────────────────────────────────────────────────────────────


def company_html(price="34.3", high="53.6", low="34.3", roce="38.9", pl_values=("9,319", "11,478")):
    """Minimal Screener company page. Pass '' values to mimic the blank
    /consolidated/ page served for companies without subsidiaries."""
    def li(name, *nums):
        spans = "".join(f'<span class="number">{n}</span>' for n in nums)
        return f'<li><span class="name">{name}</span><span class="nowrap value">{spans}</span></li>'

    pl_cells = "".join(f"<td>{v}</td>" for v in pl_values)
    return f"""
    <html><body>
    <div id="company-info" data-company-id="1275204" data-warehouse-id="80994894"></div>
    <h1 class="h2">Motherson Sumi Wiring India Ltd</h1>
    <ul id="top-ratios">
      {li("Market Cap", "22,747" if price else "")}
      {li("Current Price", price)}
      {li("High / Low", high, low)}
      {li("Stock P/E", "36.3" if price else "")}
      {li("Book Value", "3.26" if price else "")}
      {li("Dividend Yield", "1.66" if price else "")}
      {li("ROCE", roce)}
      {li("ROE", "32.4" if price else "")}
    </ul>
    <section id="profit-loss"><table>
      <thead><tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr></thead>
      <tbody><tr><td>Sales</td>{pl_cells}</tr><tr><td>Expenses</td><td></td><td></td></tr></tbody>
    </table></section>
    </body></html>
    """


BLANK_PAGE = company_html(price="", high="", low="", roce="", pl_values=("", ""))
FULL_PAGE = company_html()

SCREEN_HTML = """
<div class="sub">60 results found: Showing page 1 of 3</div>
<table class="data-table">
  <tr><th>S.No.</th><th>Name</th><th>Current Price</th><th>Market Capitalization</th><th>Return on capital employed</th></tr>
  <tr data-row-company-id="2726"><td>1.</td><td><a href="/company/RELIANCE/consolidated/">Reliance</a></td><td>1223.70</td><td>1655976.33</td><td>10.26</td></tr>
  <tr data-row-company-id="1275204"><td>2.</td><td><a href="/company/MSUMI/">Motherson Wiring</a></td><td>34.30</td><td>22747</td><td>38.9</td></tr>
</table>
"""


def history(closes, volumes=None, start_day=1):
    import datetime as dt

    base = dt.date(2025, 1, 1)
    dates = [(base + dt.timedelta(days=i)).isoformat() for i in range(len(closes))]
    return PriceHistory(dates=dates, closes=list(closes),
                        volumes=list(volumes) if volumes else [1000.0] * len(closes))


# ─── envelope ─────────────────────────────────────────────────────────────────


def test_envelope_wraps_plain_string_as_ok_report():
    env = to_envelope("# report")
    assert env == {"status": "ok", "partial": False, "warnings": [], "data": {"report": "# report"}}


def test_envelope_marks_missing_fields_partial():
    env = to_envelope(ToolResult(data={}, missing_fields=["price"], reason="blank"))
    assert env["status"] == "partial" and env["partial"] is True
    assert env["missing_fields"] == ["price"] and env["reason"] == "blank"


async def test_safe_turns_tool_error_into_error_envelope_with_candidates():
    async def boom():
        raise ToolError("ambiguous", "symbol_ambiguous", candidates=[{"symbol": "MSUMI"}])

    env = await server._safe(boom)()
    assert env["status"] == "error" and env["data"] is None
    assert env["error"]["type"] == "symbol_ambiguous"
    assert env["error"]["candidates"] == [{"symbol": "MSUMI"}]


def test_to_number_never_turns_blank_into_zero():
    assert to_number("") is None and to_number(None) is None and to_number("—") is None
    assert to_number("₹ 1,23,456 Cr.") == 123456 and to_number("-278") == -278 and to_number("44%") == 44


# ─── bug #1: silent empty data ────────────────────────────────────────────────


def test_page_has_data_detects_blank_consolidated_page():
    assert page_has_data(FULL_PAGE)
    assert not page_has_data(BLANK_PAGE)


def test_missing_fields_named_instead_of_silent_blanks():
    assert overview_missing_fields(parse_overview(FULL_PAGE)) == []
    missing = overview_missing_fields(parse_overview(BLANK_PAGE))
    assert {"current_price", "52_week_high", "52_week_low", "roce", "market_cap"} <= set(missing)


@pytest.fixture
def fake_pages(monkeypatch):
    """Serve pages from a dict keyed by (symbol, financial_type); None = 404."""
    pages = {}

    async def fake_get_page(symbol, financial_type):
        return pages.get((symbol, financial_type))

    monkeypatch.setattr(cp, "_get_page", fake_get_page)
    return pages


async def test_blank_consolidated_page_falls_back_to_standalone(fake_pages):
    fake_pages[("MSUMI", "consolidated")] = BLANK_PAGE
    fake_pages[("MSUMI", "standalone")] = FULL_PAGE
    env = await server.get_company_overview("MSUMI")
    assert env["status"] == "ok"
    assert env["data"]["financial_type"] == "standalone"
    assert env["data"]["current_price"] == 34.3 and env["data"]["key_ratios"]["ROCE"] == 38.9
    assert any("no consolidated financials" in w for w in env["warnings"])


async def test_page_with_no_data_anywhere_reports_partial(fake_pages):
    fake_pages[("GHOST", "consolidated")] = BLANK_PAGE
    fake_pages[("GHOST", "standalone")] = BLANK_PAGE
    env = await server.get_company_overview("GHOST")
    assert env["status"] == "partial"
    assert "current_price" in env["missing_fields"]
    assert "no data" in env["reason"].lower()
    assert env["data"]["current_price"] is None  # None, never "" or 0


# ─── bug #2: symbol resolution ────────────────────────────────────────────────

MOTHERSON_RESULTS = [
    Candidate("MOTHERSON", "Samvardhana Motherson International Ltd", "2132", True),
    Candidate("MSUMI", "Motherson Sumi Wiring India Ltd", "1275204", False),
    Candidate("ID/1275147", "Samvardhana Motherson International Ltd(Merged)", "1275147", False),
]


@pytest.fixture
def fake_search(monkeypatch):
    table = {}

    async def fake(query):
        return [Candidate(c.symbol, c.name, c.company_id, c.has_consolidated)
                for c in table.get(query.upper(), [])]

    monkeypatch.setattr(cp, "search_candidates", fake)
    return table


async def test_prefix_shortening_resolves_non_canonical_symbol(fake_search):
    fake_search["MOTHERSON"] = MOTHERSON_RESULTS
    best, alternatives = await resolve_symbol("MOTHERSONWIR")
    assert best.symbol == "MSUMI"
    assert "MOTHERSON" in [a.symbol for a in alternatives]


async def test_ambiguous_input_returns_top_three_candidates(fake_search):
    fake_search["RELIANC"] = [
        Candidate("RELIANCE", "Reliance Industries Ltd"),
        Candidate("RPOWER", "Reliance Power Ltd"),
        Candidate("RELINFRA", "Reliance Infrastructure Ltd"),
        Candidate("RCOM", "Reliance Communications Ltd"),
    ]
    with pytest.raises(ToolError) as exc:
        await resolve_symbol("RELIANC")
    assert exc.value.error_type == "symbol_ambiguous"
    assert len(exc.value.details["candidates"]) == 3


async def test_unknown_symbol_is_a_structured_not_found(fake_search):
    with pytest.raises(ToolError) as exc:
        await resolve_symbol("ZZQQXX")
    assert exc.value.error_type == "symbol_not_found"


def test_scoring_prefers_whole_word_and_fewer_skips():
    infy = score_candidate("INFOSYS", Candidate("INFY", "Infosys Ltd"))
    hcl = score_candidate("INFOSYS", Candidate("HCL-INSYS", "HCL Infosystems Ltd"))
    assert infy > hcl
    bajfin = score_candidate("BAJAJFINANCE", Candidate("BAJFINANCE", "Bajaj Finance Ltd"))
    housing = score_candidate("BAJAJFINANCE", Candidate("BAJAJHFL", "Bajaj Housing Finance Ltd"))
    assert bajfin > housing


async def test_overview_reports_interpreted_symbol(fake_pages, fake_search):
    fake_search["MOTHERSON"] = MOTHERSON_RESULTS
    fake_pages[("MSUMI", "consolidated")] = FULL_PAGE
    env = await server.get_company_overview("MOTHERSONWIR")
    assert env["status"] == "ok"
    assert env["meta"]["interpreted_as"] == "MSUMI"
    assert env["meta"]["requested_symbol"] == "MOTHERSONWIR"
    assert "interpreted as MSUMI" in env["warnings"][0]


async def test_overview_ambiguous_symbol_is_error_with_candidates(fake_pages, fake_search):
    fake_search["TATAMOTOR"] = [
        Candidate("TATAMTRDVR", "Tata Motors-DVR"),
        Candidate("TMCV", "Tata Motors Ltd"),
        Candidate("TMPV", "Tata Motors Passenger Vehicles Ltd"),
    ]
    env = await server.get_company_overview("TATAMOTOR")
    assert env["status"] == "error"
    assert {c["symbol"] for c in env["error"]["candidates"]} == {"TATAMTRDVR", "TMCV", "TMPV"}


# ─── bug #3: implausible ratios ───────────────────────────────────────────────

YEARS = ["Mar 2023", "Mar 2024", "Mar 2025"]


def ratio_rows(debtor, inventory, payable, ccc):
    return [
        {"label": "Debtor Days", "values": debtor},
        {"label": "Inventory Days", "values": inventory},
        {"label": "Days Payable", "values": payable},
        {"label": "Cash Conversion Cycle", "values": ccc},
        {"label": "ROCE %", "values": ["20%", "22%", "25%"]},
    ]


def test_plausible_ratios_are_not_flagged():
    rows = ratio_rows(["41", "39", "49"], ["95", "76", "77"], ["73", "62", "71"], ["63", "53", "55"])
    assert check_ratio_history(YEARS, rows) == {}


def test_parse_artifact_ccc_is_flagged_not_dropped():
    # the real-world case: CCC -278 next to Days Payable 345
    rows = ratio_rows(["30", "30", "40"], ["40", "37", "30"], ["60", "345", "65"], ["10", "-278", "5"])
    flags = check_ratio_history(YEARS, rows)
    ccc = flags["Cash Conversion Cycle"]
    assert [f["period"] for f in ccc] == ["Mar 2024"]
    assert ccc[0]["data_quality_flag"] is True and ccc[0]["value"] == -278


def test_ccc_inconsistent_with_components_is_flagged():
    rows = ratio_rows(["30", "30", "30"], ["40", "40", "40"], ["20", "20", "20"], ["50", "50", "150"])
    flags = check_ratio_history(YEARS, rows)
    assert [f["period"] for f in flags["Cash Conversion Cycle"]] == ["Mar 2025"]
    assert "inconsistent" in flags["Cash Conversion Cycle"][0]["reason"]


# ─── feature #5: technical screening ──────────────────────────────────────────


@pytest.mark.parametrize("clause,metric,op,value", [
    ("52 week low distance < 10", "pct_above_52w_low", "<", 10),
    ("52-week high distance > 30", "pct_below_52w_high", ">", 30),
    ("RSI < 30", "rsi14", "<", 30),
    ("RSI(14) <= 35", "rsi14", "<=", 35),
    ("Volume vs 20-day average volume > 2", "volume_vs_20d_avg", ">", 2),
    ("Price vs 200 DMA < -10", "pct_vs_dma200", "<", -10),
])
def test_numeric_technical_clauses(clause, metric, op, value):
    fundamental, technical = split_query(clause)
    assert fundamental == []
    assert (technical[0].metric, technical[0].op, technical[0].value) == (metric, op, value)


@pytest.mark.parametrize("clause,lhs,op,rhs", [
    ("Price above 200 DMA", "price", ">", "dma200"),
    ("Current price < DMA 50", "price", "<", "dma50"),
    ("price below 20 day moving average", "price", "<", "dma20"),
    ("50 DMA above 200 DMA", "dma50", ">", "dma200"),
])
def test_level_technical_clauses(clause, lhs, op, rhs):
    _, technical = split_query(clause)
    t = technical[0]
    assert (t.metric, t.lhs, t.op, t.rhs) == ("level", lhs, op, rhs)


def test_fundamental_clauses_pass_through_untouched():
    fundamental, technical = split_query(
        "Return on capital employed > 15 AND Current price > 100 AND 52 week low distance < 10"
    )
    assert fundamental == ["Return on capital employed > 15", "Current price > 100"]
    assert [t.metric for t in technical] == ["pct_above_52w_low"]


def test_technical_clauses_reject_or_groups():
    with pytest.raises(ToolError):
        split_query("Debt to equity < 0.5 OR Market Capitalization > 100 AND RSI < 30")


def test_indicator_math():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert rsi([float(i) for i in range(1, 30)]) == 100.0
    assert rsi([float(30 - i) for i in range(30)]) == pytest.approx(0.0)
    closes = [100.0] * 240 + [80.0] * 9 + [82.0]
    vols = [1000.0] * 249 + [5000.0]
    t, _ = compute_technicals(history(closes, vols), today=None)
    assert t["low_52w"] == 80 and t["high_52w"] == 100
    assert t["pct_above_52w_low"] == 2.5 and t["pct_below_52w_high"] == 18.0
    assert t["volume_vs_20d_avg"] == 5.0
    assert t["price"] < t["dma200"]


def test_parse_screen_results_exposes_company_ids_and_pages():
    d = parse_screen_results(SCREEN_HTML)
    assert d["total_results"] == 60 and d["total_pages"] == 3
    assert [c["_company_id"] for c in d["companies"]] == ["2726", "1275204"]


async def test_run_technical_screen_filters_and_flags_truncation(monkeypatch):
    cands = [
        {"symbol": "NEARLOW", "name": "Near Low", "company_id": "1", "fundamentals": {}},
        {"symbol": "FARLOW", "name": "Far Low", "company_id": "2", "fundamentals": {}},
        {"symbol": "NOHIST", "name": "No History", "company_id": "3", "fundamentals": {}},
    ]

    async def fake_candidates(**kwargs):
        return cands, 500  # 500 available, 3 scanned → partial

    series = {
        "1": history([100.0] * 200 + [80.0] * 49 + [82.0]),
        "2": history([80.0] * 200 + [100.0] * 50),
    }

    async def fake_history(company_id, days=365):
        if company_id not in series:
            raise RuntimeError("chart API down")
        return series[company_id]

    monkeypatch.setattr(tt, "fetch_candidates", fake_candidates)
    monkeypatch.setattr(tt, "fetch_price_history", fake_history)

    _, technical = split_query("52 week low distance < 10")
    result = await tt.run_technical_screen(["Return on capital employed > 15"], technical)
    env = to_envelope(result)
    assert [r["symbol"] for r in env["data"]["results"]] == ["NEARLOW"]
    assert env["partial"] is True
    assert "3 of 500" in env["reason"] and "could not be fetched" in env["reason"]
    assert any("NOHIST" in w for w in env["warnings"])


async def test_screen_stocks_technical_only_uses_index_universe(monkeypatch):
    seen = {}

    async def fake_candidates(query=None, index_slug=None, **kwargs):
        seen["query"], seen["slug"] = query, index_slug
        return [], 50

    monkeypatch.setattr(tt, "fetch_candidates", fake_candidates)
    env = await server.screen_stocks("RSI < 30", universe="nifty50")
    assert seen == {"query": None, "slug": "NIFTY"}
    assert env["data"]["source"]["universe"] == "nifty50"


# ─── feature #7: sector mapping ───────────────────────────────────────────────


def test_sector_index_mapping():
    assert sector_index_for(["Automobile and Auto Components", "Auto Components & Equipments"])[0] == "auto"
    assert sector_index_for(["Capital Goods", "Aerospace & Defense"])[0] == "defence"
    assert sector_index_for(["Financial Services", "Private Sector Bank"])[0] == "private_bank"
    assert sector_index_for(["Something Unmapped"]) is None
    assert resolve_index("Nifty 500")[1] == "CNX500" and resolve_index("banknifty")[0] == "bank"


# ─── bug #4: index freshness ──────────────────────────────────────────────────


async def test_freshness_reports_unknown_for_legacy_index(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "_MANIFEST_PATH", tmp_path / "manifest.json")
    f = await rag.freshness("TCS_2024_annual", "https://x/ar.pdf")
    assert f["last_indexed_at"] is None and f["source_changed"] is None


async def test_freshness_reports_timestamp_hash_and_url_change(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "_MANIFEST_PATH", tmp_path / "manifest.json")
    rag._save_manifest_entry("TCS_2024_annual", {
        "source_url": "https://x/ar.pdf",
        "last_indexed_at": "2026-01-01T00:00:00+00:00",
        "content_sha256": "abc",
        "etag": None, "last_modified": None, "content_length": None,
    })
    same = await rag.freshness("TCS_2024_annual", "https://x/ar.pdf")
    assert same["last_indexed_at"] == "2026-01-01T00:00:00+00:00"
    assert same["content_sha256"] == "abc"
    assert same["source_changed"] is None  # no validators recorded → can't tell
    moved = await rag.freshness("TCS_2024_annual", "https://x/ar-revised.pdf")
    assert moved["source_changed"] is True


# ─── NSE-backed tools: failure must never look like "no data" ─────────────────

from screener_mcp.core import nse_client as nse_mod  # noqa: E402
from screener_mcp.tools import announcements as ann_mod, shareholders as sh_mod  # noqa: E402


class FakeNSE:
    def __init__(self, announcements=None, fail=None, bulk=None):
        self._ann, self._fail, self._bulk = announcements, fail, bulk

    async def get_announcements(self, symbol):
        if self._fail:
            raise nse_mod.NSEError(self._fail)
        return self._ann or []

    async def get_bulk_deals(self, from_date, to_date, symbol=None):
        return self._bulk


@pytest.fixture
def nse_symbol_ok(monkeypatch):
    async def fake_resolve(symbol):
        return symbol.upper(), [], {"symbol": symbol.upper()}

    monkeypatch.setattr(ann_mod, "resolve_nse_symbol", fake_resolve)
    monkeypatch.setattr(sh_mod, "resolve_nse_symbol", fake_resolve)


async def test_nse_block_is_an_error_not_an_empty_result(monkeypatch, nse_symbol_ok):
    async def fake_client():
        return FakeNSE(fail="NSE API returned HTTP 403 for /api/corporate-announcements")

    monkeypatch.setattr(ann_mod, "get_nse_client", fake_client)
    env = await server.get_company_announcements("TCS")
    assert env["status"] == "error"
    assert env["error"]["type"] == "upstream_unavailable" and "403" in env["reason"]


async def test_nse_genuinely_empty_is_ok_with_warning(monkeypatch, nse_symbol_ok):
    async def fake_client():
        return FakeNSE(announcements=[])

    monkeypatch.setattr(ann_mod, "get_nse_client", fake_client)
    env = await server.get_company_announcements("TCS")
    assert env["status"] == "ok" and env["data"]["announcements"] == []
    assert "request succeeded" in env["warnings"][0]


async def test_bulk_deals_with_failed_days_is_partial(monkeypatch, nse_symbol_ok):
    rows = [{"date": "01-Sep-2026", "buySell": "BUY", "quantityTraded": "100", "tradePrice": "10", "clientName": "X"}]

    async def fake_client():
        return FakeNSE(bulk=(rows, ["02-09-2026", "03-09-2026"], 30))

    monkeypatch.setattr(sh_mod, "get_nse_client", fake_client)
    env = await server.get_bulk_deals("TCS", days=90)
    assert env["status"] == "partial"
    assert "2 of 30 days" in env["reason"]
    assert any("30 of the requested 90" in w for w in env["warnings"])
    assert env["data"]["count"] == 1


async def test_bse_only_company_is_a_clear_error(fake_pages):
    fake_pages[("543498", "standalone")] = FULL_PAGE  # fixture page has no NSE link
    env = await server.get_insider_trading("543498")
    assert env["status"] == "error" and env["error"]["type"] == "not_on_nse"


async def test_invalid_input_is_structured_error():
    env = await server.get_company_announcements("TCS", category="gossip")
    assert env["status"] == "error" and env["error"]["type"] == "invalid_input"


def test_parse_real_screen_results_page():
    """Real (trimmed) Screener screen page — same template /screen/raw/ serves."""
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "screen_results_page2.html").read_text()
    d = parse_screen_results(html)
    assert d["total_results"] == 159 and d["total_pages"] == 7
    assert len(d["companies"]) == 3
    cand = tt._row_to_candidate(d["companies"][0])
    assert cand["company_id"] and cand["symbol"] and cand["name"]
    assert isinstance(cand["fundamentals"]["Market Capitalization"], float)


def test_level_filter_sorts_by_distance_from_dma():
    _, technical = split_query("Price above 200 DMA")
    assert tt._default_sort(technical) == ("pct_vs_dma200", True)


async def test_compare_stocks_ui_keeps_top_level_stocks_for_dashboard(fake_pages):
    fake_pages[("MSUMI", "consolidated")] = FULL_PAGE
    env = await server.compare_stocks_ui(["MSUMI"])
    assert env["status"] == "ok" and env["count"] == 1
    assert env["stocks"] == env["data"]["stocks"]


# ─── price-history cache ──────────────────────────────────────────────────────

from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402

from screener_mcp.core import technicals as tech_mod  # noqa: E402

_IST = _tz(_td(hours=5, minutes=30))


def _ts(y, mo, d, h, mi):
    return _dt(y, mo, d, h, mi, tzinfo=_IST).timestamp()


@pytest.mark.parametrize("fetched,now,fresh", [
    (_ts(2026, 9, 24, 18, 0), _ts(2026, 9, 25, 8, 0), True),     # after close → valid until next open
    (_ts(2026, 9, 24, 18, 0), _ts(2026, 9, 25, 9, 20), False),   # next session opened
    (_ts(2026, 9, 25, 10, 0), _ts(2026, 9, 25, 10, 10), True),   # intraday, within 15 min
    (_ts(2026, 9, 25, 10, 0), _ts(2026, 9, 25, 10, 20), False),  # intraday, stale
    (_ts(2026, 9, 25, 15, 0), _ts(2026, 9, 25, 17, 0), False),   # fetched intraday, now closed
    (_ts(2026, 9, 25, 17, 0), _ts(2026, 9, 27, 12, 0), True),    # Friday close → valid all weekend
])
def test_price_cache_freshness(fetched, now, fresh):
    assert tech_mod.cache_is_fresh(fetched, now) is fresh


async def test_price_cache_serves_repeat_and_shorter_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(tech_mod, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(tech_mod, "cache_is_fresh", lambda *a: True)
    tech_mod.clear_price_cache()
    calls = []
    payload = {"datasets": [{"metric": "Price", "values": [[f"2025-{m:02d}-01", str(100 + m)] for m in range(1, 13)]}]}

    class FakeClient:
        async def get_json(self, path, params=None):
            calls.append(params["days"])
            return payload

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(tech_mod, "get_client", fake_client)
    h1 = await tech_mod.fetch_price_history("42", 400)
    h2 = await tech_mod.fetch_price_history("42", 400)
    tech_mod.clear_price_cache()                           # memory gone → disk hit
    h3 = await tech_mod.fetch_price_history("42", 120)     # shorter window → trimmed from cache
    assert calls == ["400"]
    assert len(h1.closes) == len(h2.closes) == 12
    assert h3.dates[0] > "2025-08-01" and h3.closes[-1] == 112


# ─── sector awareness ─────────────────────────────────────────────────────────

from screener_mcp.core.quality import is_financial  # noqa: E402


def test_financials_skip_days_checks():
    assert is_financial(["Financial Services", "Private Sector Bank"])
    assert not is_financial(["Automobile and Auto Components"])
    rows = ratio_rows(["30", "30", "40"], ["40", "37", "30"], ["60", "345", "65"], ["10", "-278", "5"])
    assert check_ratio_history(YEARS, rows, financial=True) == {}


# ─── commodities ──────────────────────────────────────────────────────────────

from screener_mcp.tools import commodities as com_mod  # noqa: E402


async def test_commodity_benchmark_with_inr_conversion(monkeypatch):
    async def fake_chart(symbol, years):
        if symbol == "INR=X":
            return {"meta": {"regularMarketPrice": 90.0}, "closes": [90.0], "timestamps": []}
        return {"meta": {"regularMarketPrice": 3110.35, "regularMarketTime": 1790000000},
                "closes": [1555.175] * 60 + [3110.35], "timestamps": []}

    monkeypatch.setattr(com_mod, "_yahoo_chart", fake_chart)
    env = await server.get_commodity_prices("gold", years=2)
    b = env["data"]["benchmark"]
    assert env["status"] == "ok" and b["source_symbol"] == "GC=F"
    assert b["change_period_pct"] == 100.0
    assert b["approx_inr"] == pytest.approx(3110.35 * 10 / 31.1035 * 90, rel=1e-6)  # ₹/10g


async def test_commodity_without_feed_is_partial():
    env = await server.get_commodity_prices("nickel")
    assert env["status"] == "partial" and env["missing_fields"] == ["benchmark_price"]


# ─── structured outputs / dashboard ───────────────────────────────────────────

from screener_mcp.parsers.company import debt_to_equity  # noqa: E402


def test_debt_to_equity_from_balance_sheet():
    html = """<section id="balance-sheet"><table><thead><tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr></thead>
      <tbody><tr><td>Equity Capital</td><td>10</td><td>10</td></tr><tr><td>Reserves</td><td>80</td><td>90</td></tr>
      <tr><td>Borrowings +</td><td>50</td><td>25</td></tr></tbody></table></section>"""
    assert debt_to_equity(html) == 0.25
    assert debt_to_equity("<html></html>") is None


async def test_dashboard_widget_is_an_mcp_app_resource():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    uri = tools["compare_stocks_ui"].meta["ui"]["resourceUri"]
    resources = {str(r.uri): r for r in await server.mcp.list_resources()}
    assert resources[uri].mimeType == "text/html;profile=mcp-app"
    html = list(await server.mcp.read_resource(uri))[0].content
    assert "/*__EXT_APPS_BUNDLE__*/" not in html and "globalThis.ExtApps={" in html


async def test_quarterly_results_are_structured(fake_pages):
    page = FULL_PAGE.replace("</body>", """<section id="quarters"><table>
      <thead><tr><th></th><th>Mar 2026</th><th>Jun 2026</th></tr></thead>
      <tbody><tr><td>Sales +</td><td>3,335</td><td>3,407</td></tr><tr><td>OPM %</td><td>11%</td><td>12%</td></tr></tbody>
      </table></section></body>""")
    fake_pages[("MSUMI", "consolidated")] = page
    env = await server.get_quarterly_results("MSUMI")
    assert env["data"]["quarters"] == ["Mar 2026", "Jun 2026"]
    assert env["data"]["rows"][0]["values"] == [3335.0, 3407.0]
    assert env["data"]["rows"][1]["values"] == [11.0, 12.0]


async def test_portfolio_pnl_ignores_holdings_without_price(tmp_path, monkeypatch):
    from screener_mcp.tools import portfolio as pf

    monkeypatch.setattr(pf, "_PORTFOLIO_PATH", tmp_path / "p.json")
    pf._save({"holdings": {"AAA": {"quantity": 10, "avg_price": 100}, "BBB": {"quantity": 5, "avg_price": 1000}}})

    async def fake_price(sym):
        return 110.0 if sym == "AAA" else None

    monkeypatch.setattr(pf, "_live_price", fake_price)
    env = await server.get_portfolio()
    t = env["data"]["totals"]
    assert env["status"] == "partial" and env["missing_fields"] == ["BBB.price"]
    assert t["pnl"] == 100.0 and t["pnl_pct"] == 10.0   # not -4900 from counting BBB as worthless
