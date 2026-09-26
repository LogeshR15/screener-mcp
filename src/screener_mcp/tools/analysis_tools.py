"""
Higher-level analysis tools that combine several data sources.

  get_full_analysis — every table for a company in one call, built from the
                      same helpers as the standalone tools so the two always
                      agree (years, quarters, peers).
  get_red_flags     — rule-based checks over the full history, each with the
                      numbers behind it.
"""

import asyncio

from ..core import history as hist
from ..core.company_page import CompanyPage, fetch_company_page
from ..core.envelope import ToolResult
from ..core.quality import OVERVIEW_CORE_FIELDS, explain_missing, is_financial, overview_missing_fields
from ..parsers.company import parse_balance_sheet, parse_cash_flow, parse_overview, parse_profit_loss, parse_ratios
from .company_tools import QUARTERS, SHAREHOLDING_QUARTERS, peers_data, quarterly_data, shareholding_data, statement_data

FULL_ANALYSIS_YEARS = 10


async def get_full_analysis(symbol: str, financial_type: str = "consolidated") -> ToolResult:
    """Every table for a company in one call, as one readable report."""
    page = await fetch_company_page(symbol, financial_type)
    ov = parse_overview(page.html)

    statements = ("profit_loss", "balance_sheet", "cash_flow", "ratios")
    (tables, (quarters, peers)) = await asyncio.gather(
        asyncio.gather(*[statement_data(page, s, FULL_ANALYSIS_YEARS) for s in statements]),
        asyncio.gather(_quarters(page), peers_data(page)),
    )
    shareholding, sh_missing, _ = shareholding_data(page)

    warnings = list(page.warnings)
    missing = overview_missing_fields(ov)
    sections = [
        f"# {ov.get('name', page.symbol)} — Full Analysis Data [{page.financial_type}]",
        f"Symbol: {page.symbol} | NSE: {ov.get('nse_code') or '—'} | BSE: {ov.get('bse_code') or '—'}",
        f"Sectors: {', '.join(ov.get('sectors', [])) or '—'}",
        f"Current Price: {ov.get('current_price') or '— (not on source page)'}",
        f"52W High: {ov.get('52_week_high') or '—'} | 52W Low: {ov.get('52_week_low') or '—'}",
        "",
        "## Key Ratios",
        *[f"  {k}: {v or '— (not on source page)'}" for k, v in ov.get("key_ratios", {}).items()],
    ]
    if ov.get("about"):
        sections += ["", f"## About\n{ov['about']}"]

    for statement, (data, w, m, _) in zip(statements, tables):
        warnings += w
        missing += m
        sections += ["", _fmt_table(data["title"], data["years"], data["rows"], data.get("ttm"))]
        if statement == "ratios" and data.get("row_sources"):
            computed = {k: v for k, v in data["row_sources"].items() if not v.startswith("Screener.in ratios")}
            if computed:
                sections.append("Sources: " + "; ".join(f"{k} — {v}" for k, v in computed.items()))
        if statement == "ratios" and data.get("data_quality_flags"):
            sections.append("⚠ DATA QUALITY — rows marked data_quality_flag hold implausible values; treat as suspect.")

    q_data, q_missing = quarters
    missing += q_missing
    sections += ["", _fmt_table("Quarterly Results (₹ Crore)", q_data["quarters"], q_data["rows"])]

    missing += sh_missing
    sections += ["", _fmt_table("Shareholding Pattern (%)", shareholding["quarters"], shareholding["rows"])]
    if shareholding.get("promoter_group") and not shareholding["promoter_group"]["present"]:
        sections.append(shareholding["promoter_group"]["note"])

    p_data, p_missing, _ = peers
    missing += p_missing
    sections += ["", _fmt_peers(p_data)]

    reason = explain_missing(missing, all(m in missing for m in OVERVIEW_CORE_FIELDS)) if missing else None
    return ToolResult(
        data={
            "report": "\n".join(sections),
            "coverage": {"fiscal_years": FULL_ANALYSIS_YEARS, "quarters": QUARTERS,
                         "shareholding_quarters": SHAREHOLDING_QUARTERS,
                         "note": "Same windows as get_financials(years=10), get_quarterly_results and "
                                 "get_shareholding_pattern; the P&L's TTM column is shown separately."},
        },
        warnings=warnings,
        missing_fields=missing,
        reason=reason,
        meta=page.meta,
    )


async def _quarters(page: CompanyPage) -> tuple[dict, list[str]]:
    data, missing, _ = quarterly_data(page)
    return data, missing


# ─── red flags ────────────────────────────────────────────────────────────────

def _flag(check: str, severity: str, finding: str, **evidence) -> dict:
    return {"check": check, "severity": severity, "finding": finding, "evidence": evidence}


def _latest(series: dict) -> tuple[str | None, float | None]:
    items = [(k, v) for k, v in series.items() if v is not None]
    return items[-1] if items else (None, None)


def _ago(series: dict, n: int) -> tuple[str | None, float | None]:
    """The value n fiscal years before the latest non-blank one."""
    items = [(k, v) for k, v in series.items() if v is not None]
    return items[-1 - n] if len(items) > n else (None, None)


def red_flag_checks(page: CompanyPage) -> dict:
    """Rule-based red-flag checks over a company page's full history."""
    ov = parse_overview(page.html)
    financial = is_financial(ov.get("sectors", []))
    pl, bs, cf, ratios = (parse_profit_loss(page.html), parse_balance_sheet(page.html),
                          parse_cash_flow(page.html), parse_ratios(page.html))
    sales, profit = hist.series(pl, "sales") or hist.series(pl, "revenue"), hist.series(pl, "net profit")
    pbt, other_income = hist.series(pl, "profit before tax"), hist.series(pl, "other income")
    cfo = hist.series(cf, "cash from operating")
    roce = hist.series(ratios, "roce")
    debtor_days, inventory_days = hist.series(ratios, "debtor days"), hist.series(ratios, "inventory days")
    borrowings, equity_capital = hist.series(bs, "borrowings"), hist.series(bs, "equity capital")
    de = hist.debt_to_equity_by_year(bs)
    shareholding, _, _ = shareholding_data(page)

    flags, clean, skipped = [], [], []

    # 1-2. Promoter holding and pledge
    promoter = shareholding.get("promoter_group") or {}
    if not promoter:
        skipped.append({"check": "promoter_holding", "why": "no shareholding table on the source page"})
    elif not promoter.get("present"):
        skipped.append({"check": "promoter_holding", "why": promoter.get("note")})
    else:
        t = promoter.get("trend") or {}
        drop = -(t.get("change_pp") or 0)
        if drop > 5:
            flags.append(_flag("promoter_holding", "high", f"Promoter holding fell {drop:.1f} pp over {t['over_quarters']} quarters.", **t))
        elif drop > 2:
            flags.append(_flag("promoter_holding", "medium", f"Promoter holding fell {drop:.1f} pp over {t['over_quarters']} quarters.", **t))
        else:
            clean.append("promoter_holding")
    pledge = shareholding.get("pledge") or {}
    if pledge.get("status") == "reported":
        p = pledge["pledged_pct_of_promoter_holding"]
        severity = "high" if p > 20 else "low"
        flags.append(_flag("pledge", severity, f"Promoters have pledged {p:g}% of their holding.", pledged_pct=p))
    elif pledge.get("status") == "not_flagged":
        clean.append("pledge")
    elif pledge:
        skipped.append({"check": "pledge", "why": pledge.get("note")})

    # 3. Leverage
    if financial:
        skipped.append({"check": "debt", "why": "lender — leverage is the business model; check capital adequacy and asset quality instead"})
    else:
        y, latest_de = _latest(de)
        sales_cagr, debt_cagr = hist.cagr(sales, 3), hist.cagr(borrowings, 3)
        if latest_de is not None and latest_de > 1.5:
            flags.append(_flag("debt", "high", f"Debt-to-equity is {latest_de:.2f} ({y}).", debt_to_equity=latest_de))
        elif latest_de is not None and latest_de > 1:
            flags.append(_flag("debt", "medium", f"Debt-to-equity is {latest_de:.2f} ({y}).", debt_to_equity=latest_de))
        if debt_cagr is not None and sales_cagr is not None and debt_cagr > sales_cagr + 15 and (latest_de or 0) > 0.3:
            flags.append(_flag("debt_growth", "medium",
                               f"Borrowings grew {debt_cagr:.0f}%/yr over 3 years vs sales {sales_cagr:.0f}%/yr.",
                               borrowings_cagr_3y=debt_cagr, sales_cagr_3y=sales_cagr))
        if not any(f["check"].startswith("debt") for f in flags):
            clean.append("debt")

    # 4. ROCE
    if financial:
        skipped.append({"check": "roce", "why": "not meaningful for lenders"})
    else:
        (y1, r1), (y0, r0) = _latest(roce), _ago(roce, 3)
        if r1 is not None and r0 is not None and r1 < r0 - 5:
            flags.append(_flag("roce_trend", "medium", f"ROCE fell from {r0:.0f}% ({y0}) to {r1:.0f}% ({y1}).", roce_then=r0, roce_now=r1))
        if r1 is not None and r1 < 10:
            flags.append(_flag("roce_level", "medium", f"ROCE is {r1:.0f}% ({y1}) — below a typical cost of capital.", roce=r1))
        if r1 is not None and not any(f["check"].startswith("roce") for f in flags):
            clean.append("roce")

    # 5. Cash flow vs profit
    if financial:
        skipped.append({"check": "cash_conversion", "why": "operating cash flow of a lender swings with loan growth — not a quality signal"})
    else:
        years = [y for y in profit if y in cfo and profit[y] is not None and cfo[y] is not None][-5:]
        total_p, total_c = sum(profit[y] for y in years), sum(cfo[y] for y in years)
        if len(years) >= 3 and total_p > 0:
            ratio = round(total_c / total_p, 2)
            if ratio < 0.5:
                flags.append(_flag("cash_conversion", "high", f"Operating cash flow was {ratio:.0%} of net profit over {len(years)} years.", cfo_to_profit=ratio, years=years))
            elif ratio < 0.8:
                flags.append(_flag("cash_conversion", "medium", f"Operating cash flow was {ratio:.0%} of net profit over {len(years)} years.", cfo_to_profit=ratio, years=years))
            else:
                clean.append("cash_conversion")
        y, c = _latest({k: cfo.get(k) for k in profit})
        if c is not None and c < 0 and (profit.get(y) or 0) > 0:
            flags.append(_flag("negative_cfo", "high", f"Negative operating cash flow in {y} despite a profit.", cfo=c, net_profit=profit.get(y)))

    # 6. Revenue growth without profit growth
    s3, p3 = hist.cagr(sales, 3), hist.cagr(profit, 3)
    if s3 is not None and p3 is not None:
        if s3 > 5 and p3 < s3 - 10:
            flags.append(_flag("growth_quality", "medium", f"Sales grew {s3:.0f}%/yr over 3 years but profit only {p3:.0f}%/yr.", sales_cagr_3y=s3, profit_cagr_3y=p3))
        else:
            clean.append("growth_quality")

    # 7. Working capital
    for check, s in (("debtor_days", debtor_days), ("inventory_days", inventory_days)):
        (y1, d1), (y0, d0) = _latest(s), _ago(s, 3)
        if financial or d1 is None or d0 is None:
            continue
        if d1 > d0 + 20 and d1 > d0 * 1.3:
            flags.append(_flag(check, "medium", f"{check.replace('_', ' ').title()} rose from {d0:.0f} ({y0}) to {d1:.0f} ({y1}).", then=d0, now=d1))
        else:
            clean.append(check)

    # 8. Profit leaning on other income (a lender's fee income is core business)
    y, oi = _latest(other_income)
    if financial:
        skipped.append({"check": "other_income", "why": "for lenders, other income is mostly fees — core revenue"})
    elif oi is not None and pbt.get(y):
        share = oi / pbt[y]
        if pbt[y] > 0 and share > 0.25:
            flags.append(_flag("other_income", "medium",
                               f"Other income was {share:.0%} of profit before tax in {y} — check for one-offs.",
                               other_income=oi, profit_before_tax=pbt[y]))
        else:
            clean.append("other_income")
    spikes = [] if financial else [
        yy for yy in pbt if pbt.get(yy) and other_income.get(yy) and pbt[yy] > 0 and other_income[yy] / pbt[yy] > 0.25]
    if spikes and not any(f["check"] == "other_income" for f in flags):
        flags.append(_flag("other_income_history", "low", f"Other income exceeded 25% of profit before tax in {', '.join(spikes[-3:])}.", years=spikes[-3:]))

    # 9. Dilution
    (y1, e1), (y0, e0) = _latest(equity_capital), _ago(equity_capital, 3)
    if e1 and e0 and e1 > e0 * 1.1:
        flags.append(_flag("dilution", "low", f"Equity capital rose {e1 / e0 - 1:.0%} from {y0} to {y1} — new shares issued (or a split/bonus).", then=e0, now=e1))
    elif e1 and e0:
        clean.append("dilution")

    order = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: order[f["severity"]])
    counts = {s: sum(f["severity"] == s for f in flags) for s in order}
    return {
        "symbol": page.symbol,
        "financial_type": page.financial_type,
        "financial_company": financial,
        "summary": counts,
        "flags": flags,
        "clean_checks": sorted(set(clean)),
        "skipped_checks": skipped,
        "not_checked": [
            "contingent liabilities", "auditor remarks / qualifications", "related-party transactions",
            "promoter / KMP changes",
        ],
        "not_checked_note": "These need the annual report — use ask_company_research(symbol, question, doc_type='annual_report').",
        "thresholds": {
            "promoter_holding": "drop > 2 pp (medium) / > 5 pp (high) over 8 quarters",
            "pledge": "> 0% (low) / > 20% (high)",
            "debt": "D/E > 1 (medium) / > 1.5 (high); borrowings 3y CAGR > sales CAGR + 15 pp",
            "roce": "fell > 5 pp over 3 years, or below 10%",
            "cash_conversion": "operating cash flow < 80% (medium) / < 50% (high) of net profit over 5 years",
            "growth_quality": "3y profit CAGR more than 10 pp below sales CAGR",
            "working_capital": "debtor or inventory days up > 20 days and > 30% over 3 years",
            "other_income": "> 25% of profit before tax",
            "dilution": "equity capital up > 10% over 3 years",
        },
    }


async def get_red_flags(symbol: str, financial_type: str = "consolidated") -> ToolResult:
    page = await fetch_company_page(symbol, financial_type)
    data = red_flag_checks(page)
    missing = [] if parse_profit_loss(page.html).get("rows") else ["profit_loss"]
    return ToolResult(
        data=data,
        warnings=list(page.warnings),
        missing_fields=missing,
        reason="No statements on the source page — the checks had nothing to run on." if missing else None,
        meta=page.meta,
    )


# ─── formatting helpers ────────────────────────────────────────────────────────

def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return f"{int(v):,}"
    return f"{v:,}" if isinstance(v, (int, float)) else str(v)


def _fmt_table(title: str, years: list[str], rows: list[dict], ttm: dict | None = None) -> str:
    if not years or not rows:
        return f"## {title}\nNo data available."
    cols = years + (["TTM"] if ttm else [])
    lines = [f"## {title}", "", f"{'Metric':<28} " + "  ".join(f"{c:>10}" for c in cols)]
    lines.append("-" * (29 + 12 * len(cols)))
    for row in rows:
        values = list(row.get("values", []))[-len(years):]
        values += [None] * (len(years) - len(values))
        if ttm:
            values.append(ttm.get(row["label"]))
        lines.append(f"{row['label'][:28]:<28} " + "  ".join(f"{_cell(v):>10}" for v in values))
    return "\n".join(lines)


def _fmt_peers(data: dict) -> str:
    rows, columns = data.get("rows", []), data.get("columns", [])
    if not rows:
        ctx = data.get("sector_context")
        extra = ("\n" + "\n".join(f"  {k}: {v}" for k, v in ctx.items())) if ctx else ""
        return "## Peer Comparison\nNo peer rows returned by Screener.in." + extra
    widths = {c: min(20, max(len(c), *(len(_cell(r.get(c))) for r in rows))) for c in columns}
    lines = ["## Peer Comparison", "",
             "  ".join(f"{c[:widths[c]]:{widths[c]}}" for c in columns),
             "  ".join("-" * widths[c] for c in columns)]
    for r in rows:
        lines.append("  ".join(f"{_cell(r.get(c))[:widths[c]]:{widths[c]}}" for c in columns))
    return "\n".join(lines)
