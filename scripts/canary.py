"""
Live canary: does the server still understand Screener.in / NSE?

Screener changes its HTML without notice, and a parser that silently stops
matching is exactly how blank-but-"ok" data happened before. This script calls
the real tools against a few known companies and asserts the results still
look right. It runs daily in CI (.github/workflows/canary.yml) with no
credentials; a failure opens a GitHub issue.

  python scripts/canary.py            # exit 1 if any hard check fails

NSE checks are soft (reported, never failing the run): NSE routinely blocks
cloud IPs such as GitHub's runners, which says nothing about our parsers.
"""

import asyncio
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("SCREENER_PRICE_CACHE", "0")  # always hit the live site

from screener_mcp import server  # noqa: E402


def _check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


def _ok(env: dict, what: str) -> dict:
    """Assert a tool envelope succeeded; return its data."""
    if env.get("status") == "error":
        err = env.get("error") or {}
        raise AssertionError(f"{what}: error {err.get('type')} — {env.get('reason')}")
    return env.get("data") or {}


async def screener_reachable():
    """Preflight: if Screener itself is unreachable (outage, or the runner's IP is
    blocked), say so once instead of failing every parser check confusingly."""
    import httpx

    from screener_mcp.client import get_client

    client = await get_client()
    try:
        await client.get_html("/company/TCS/")
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        raise AssertionError(
            f"Screener.in unreachable ({type(e).__name__}: {e}). This is connectivity or blocking, "
            "not a parser change — parser checks were skipped."
        )


async def overview_consolidated():
    env = await server.get_company_overview("TCS")
    d = _ok(env, "TCS overview")
    _check(env["status"] == "ok", f"TCS overview partial — missing {env.get('missing_fields')}")
    _check(d["current_price"] and d["52_week_low"] and d["key_ratios"].get("ROCE"), "TCS price/52W/ROCE blank")
    _check(d["financial_type"] == "consolidated", "TCS should be served consolidated")
    _check(bool(d.get("about")), "TCS about text no longer parsed")


async def overview_standalone_fallback():
    env = await server.get_company_overview("MSUMI")
    d = _ok(env, "MSUMI overview")
    _check(env["status"] == "ok", f"MSUMI overview partial — missing {env.get('missing_fields')}")
    _check(d["financial_type"] == "standalone",
           "MSUMI (no subsidiaries) should fall back to standalone — blank-page detection may be broken")


async def financial_tables():
    pl = _ok(await server.get_financials("TCS", "profit_loss"), "P&L")
    _check(len(pl.get("rows", [])) >= 8, "P&L table missing or short")
    ratios = _ok(await server.get_financials("TCS", "ratios"), "ratios")
    labels = {r["label"] for r in ratios.get("rows", [])}
    _check({"Debtor Days", "ROCE %"} <= labels, f"ratios rows changed: {sorted(labels)}")
    _check({"ROE %", "P/E (year-end)", "P/B (year-end)"} <= labels,
           f"computed ratio rows missing (chart API or balance sheet changed?): {sorted(labels)}")
    _check("TTM" not in pl["years"] and pl.get("ttm"), "TTM column no longer split from fiscal years")
    _check(not any(r["label"].endswith("+") for r in pl["rows"]), "row labels carry '+' again")
    env = await server.get_quarterly_results("TCS")
    q = _ok(env, "quarterly results")
    _check(env["status"] == "ok" and q.get("quarters"), f"quarterly results {env['status']}: {env.get('reason')}")


async def symbol_resolution():
    env = await server.get_company_overview("MOTHERSONWIR")
    _ok(env, "MOTHERSONWIR resolution")
    _check(env.get("meta", {}).get("interpreted_as") == "MSUMI",
           f"MOTHERSONWIR no longer resolves to MSUMI: {env.get('meta')}")


async def technical_screen():
    # Parsing check, not a load test: 10 price histories is enough to prove the
    # chart API still parses, and stays well under Screener's burst limit.
    d = _ok(await server.screen_stocks("RSI < 101", universe="nifty50", limit=5, max_candidates=10),
            "technical screen")
    _check(d["candidates_available"] and d["candidates_available"] >= 45,
           f"Nifty 50 constituents not parsed (got {d['candidates_available']})")
    _check(d["matches_found"] >= 8, f"price history failing ({d['matches_found']}/10 had data)")
    t = d["results"][0]["technicals"]
    _check(all(t.get(k) is not None for k in ("price", "high_52w", "dma200", "rsi14")), f"technicals incomplete: {t}")


async def sector_compare():
    env = await server.compare_to_sector("TCS", days=30)
    d = _ok(env, "compare_to_sector")
    _check(env["status"] == "ok", f"compare_to_sector partial: {env.get('reason')}")
    _check(d["sector_benchmark"]["key"] == "it", "TCS should map to Nifty IT")


async def peers():
    d = _ok(await server.get_peer_comparison("TCS"), "peers")
    _check(len(d.get("rows", [])) >= 3, "peer table empty")


async def nse_announcements():
    d = _ok(await server.get_company_announcements("TCS", days=90), "NSE announcements")
    _check(d.get("total_announcements_fetched", 0) > 0, "NSE returned no announcements for TCS")


async def news_feed():
    d = _ok(await server.get_recent_news("TCS", days=14), "news")
    _check(d.get("count", 0) > 0, "Google News returned no TCS headlines in 14 days")


async def analyst_consensus():
    env = await server.get_analyst_targets("TCS")
    d = _ok(env, "analyst targets")
    _check(d.get("consensus") and d["consensus"].get("analysts", 0) > 5, f"no TCS consensus: {env.get('warnings')}")


async def relative_valuation():
    d = _ok(await server.get_relative_valuation(["TCS"]), "relative valuation")
    r = d["results"][0]
    _check(r["industry_median_pe"] and r["industry_companies"] >= 20,
           f"industry page not parsed for TCS: {r}")


async def moat_signals():
    d = _ok(await server.get_moat_signals("MSUMI"), "moat signals")
    _check(d.get("industry") and d["industry"]["companies"] >= 50 and d["position"],
           f"industry position missing: {d.get('industry')}, {d.get('position')}")
    _check(d["durability"].get("roce"), "ROCE history not parsed")


async def shareholding_and_pledge():
    itc = _ok(await server.get_shareholding_pattern("ITC"), "ITC shareholding")
    _check(itc["promoter_group"]["present"] is False and itc["pledge"]["status"] == "not_applicable",
           f"ITC (no promoter) misread: {itc.get('promoter_group')}, {itc.get('pledge')}")
    # Pledges are read from Screener's cons list; WEBELSOLAR's promoters had ~89% pledged in Sep 2026.
    ws = _ok(await server.get_shareholding_pattern("WEBELSOLAR"), "WEBELSOLAR shareholding")
    _check(ws["pledge"]["status"] == "reported",
           f"promoter pledge no longer found on the page (cons list changed, or pledge released?): {ws['pledge']}")


async def sector_theme():
    d = _ok(await server.screen_by_theme("defense", limit=5), "defense theme")
    _check(d["universe_size"] >= 20 and all(u["companies"] for u in d["universe"]),
           f"defense universe (index / industry pages) not parsed: {d.get('universe')}")
    _check("HAL" in [r["symbol"] for r in d["results"]] or d["total_matches"] > 0,
           f"defense theme returned nothing plausible: {d['results']}")


async def forward_outlook():
    d = _ok(await server.get_forward_outlook("BEL", include_earnings_call=False), "forward outlook")
    _check(d.get("analyst_estimates") and d["analyst_estimates"]["years"], "no BEL analyst estimates")


HARD = [overview_consolidated, overview_standalone_fallback, financial_tables, symbol_resolution,
        technical_screen, sector_compare, peers, relative_valuation, moat_signals, shareholding_and_pledge,
        sector_theme]
SOFT = [nse_announcements, news_feed, analyst_consensus, forward_outlook]  # third-party feeds: warn, don't fail


async def main() -> int:
    failures, soft_failures = [], []
    try:
        await screener_reachable()
        print("PASS  screener_reachable")
        hard = HARD
    except AssertionError as e:
        print(f"FAIL  screener_reachable: {e}")
        failures.append(("screener_reachable", str(e)))
        hard = []
    for check, bucket in [(c, failures) for c in hard] + [(c, soft_failures) for c in SOFT]:
        try:
            await check()
            print(f"PASS  {check.__name__}")
        except Exception as e:
            detail = str(e) if isinstance(e, AssertionError) else traceback.format_exc(limit=3)
            bucket.append((check.__name__, detail))
            print(f"{'FAIL' if bucket is failures else 'WARN'}  {check.__name__}: {detail}")

    summary = {"failed": [n for n, _ in failures], "warnings": [n for n, _ in soft_failures]}
    report = os.getenv("CANARY_REPORT")
    if report:
        with open(report, "w") as f:
            f.write("\n".join(f"- **{n}**: {d}" for n, d in failures) or "all checks passed")
            if soft_failures:
                f.write("\n\nSoft (NSE) warnings:\n" + "\n".join(f"- {n}: {d}" for n, d in soft_failures))
    print(json.dumps(summary))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
