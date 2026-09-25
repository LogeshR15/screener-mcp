"""
Higher-level analysis tools — these fetch and combine multiple data sources
to produce analyst-grade structured output for Claude to reason over.
"""

from ..core.company_page import fetch_company_page
from ..core.envelope import ToolResult
from ..core.quality import check_ratio_history, explain_missing, overview_missing_fields, OVERVIEW_CORE_FIELDS
from ..parsers.company import parse_full_page


async def get_full_analysis(
    symbol: str,
    financial_type: str = "consolidated",
) -> ToolResult:
    """
    Fetch ALL financial data for a company in one call.

    Returns a structured text dump that Claude can reason over to:
      - Summarize for beginners
      - Identify red flags
      - Find improving trends
      - Answer specific questions

    This is the primary tool for deep-dive analysis.
    """
    page = await fetch_company_page(symbol, financial_type)
    symbol, financial_type = page.symbol, page.financial_type
    data = parse_full_page(page.html)

    sections = []

    # ── Overview ─────────────────────────────────────────────────────────────
    ov = data.get("overview", {})
    sections.append(f"# {ov.get('name', symbol)} — Full Analysis Data [{financial_type}]")
    sections.append(f"Symbol: {symbol}")
    sections.append(f"Sectors: {', '.join(ov.get('sectors', [])) or '—'}")
    sections.append(f"Current Price: {ov.get('current_price') or '— (not on source page)'}")
    sections.append(f"52W High: {ov.get('52_week_high') or '—'} | 52W Low: {ov.get('52_week_low') or '—'}")
    sections.append("")
    sections.append("## Key Ratios")
    for k, v in ov.get("key_ratios", {}).items():
        sections.append(f"  {k}: {v or '— (not on source page)'}")
    if ov.get("about"):
        sections.append("")
        sections.append(f"## About\n{ov['about']}")

    # ── Profit & Loss ────────────────────────────────────────────────────────
    pl = data.get("profit_loss", {})
    sections.append("")
    sections.append(_fmt_table("Profit & Loss (₹ Crore)", pl, n_years=10))

    # ── Balance Sheet ─────────────────────────────────────────────────────────
    bs = data.get("balance_sheet", {})
    sections.append("")
    sections.append(_fmt_table("Balance Sheet (₹ Crore)", bs, n_years=10))

    # ── Cash Flow ─────────────────────────────────────────────────────────────
    cf = data.get("cash_flow", {})
    sections.append("")
    sections.append(_fmt_table("Cash Flow (₹ Crore)", cf, n_years=10))

    # ── Quarterly Results ─────────────────────────────────────────────────────
    qr = data.get("quarterly_results", {})
    sections.append("")
    sections.append(_fmt_table("Quarterly Results (₹ Crore)", qr, n_years=8))

    # ── Ratios History ────────────────────────────────────────────────────────
    rh = data.get("ratios_history", {})
    sections.append("")
    sections.append(_fmt_table("Key Ratios History", rh, n_years=10))
    ratio_flags = check_ratio_history(rh.get("years", []), rh.get("rows", []))
    if ratio_flags:
        sections.append("")
        sections.append("⚠ DATA QUALITY — these ratio values are implausible or internally inconsistent; treat as suspect:")
        for label, flags in ratio_flags.items():
            for f in flags:
                sections.append(f"  {label} [{f['period']}] = {f['raw']}: {f['reason']}")

    # ── Shareholding ──────────────────────────────────────────────────────────
    sh = data.get("shareholding", {})
    sections.append("")
    sections.append(_fmt_shareholding(sh))

    # ── Peers ─────────────────────────────────────────────────────────────────
    peers = data.get("peers", [])
    sections.append("")
    sections.append(_fmt_peers(peers))

    warnings = list(page.warnings)
    missing = overview_missing_fields(ov)
    for key, label in (("profit_loss", "profit & loss"), ("balance_sheet", "balance sheet"),
                       ("cash_flow", "cash flow"), ("quarterly_results", "quarterly results")):
        table = data.get(key, {})
        if not any(v for r in table.get("rows", []) for v in r.get("values", [])):
            missing.append(key)
    n_flags = sum(len(f) for f in ratio_flags.values())
    if n_flags:
        warnings.append(f"{n_flags} ratio-history value(s) flagged as implausible (see DATA QUALITY section).")
    reason = None
    if missing:
        reason = explain_missing(missing, all(m in missing for m in OVERVIEW_CORE_FIELDS))
    return ToolResult(
        data={"report": "\n".join(sections), "data_quality_flags": ratio_flags or None},
        warnings=warnings,
        missing_fields=missing,
        reason=reason,
        meta=page.meta,
    )


async def get_red_flags(symbol: str, financial_type: str = "consolidated") -> ToolResult:
    """
    Fetch all company data and return a structured checklist of potential red flags.

    Checks for:
      - Declining promoter holding
      - Rising debt
      - Falling ROCE/ROE
      - Negative cash flow from operations while profits are positive
      - High pledged shares
      - Revenue growth without profit growth
      - Increasing inventory/debtor days
    """
    result = await get_full_analysis(symbol, financial_type)

    # Return the raw data with instructions for Claude
    # Claude will do the red flag reasoning on top of this
    instructions = """
---
ANALYST TASK: Using the financial data above, identify ALL potential red flags.
Check the following systematically:
1. Promoter holding trend (declining = concern)
2. Pledged % (>20% = significant concern)
3. Debt trend (rising debt with flat/falling sales = concern)
4. ROCE/ROE trend (declining = concern)
5. CFO vs PAT divergence (profits without cash = concern)
6. Revenue vs profit growth gap (revenue growing, profits not = concern)
7. Contingent liabilities (rising = concern)
8. Receivables/inventory growth vs revenue growth
9. Auditor remarks or qualifications (not parseable, flag as "check annual report")
10. Related party transactions trend

For each red flag found, explain: what it is, why it matters, and severity (High/Medium/Low).
If no red flags on a metric, confirm it's clean.
Format as a structured report with a summary verdict.
---
"""
    result.data["report"] += "\n" + instructions
    return result


async def beginner_explainer(symbol: str) -> ToolResult:
    """
    Fetch company data and prepare it for a beginner-friendly explanation.
    Claude will translate numbers into plain language.
    """
    page = await fetch_company_page(symbol, "consolidated")
    data = parse_full_page(page.html)

    ov = data.get("overview", {})
    ratios = ov.get("key_ratios", {})

    instructions = f"""
## {ov.get('name', symbol)} — Beginner Explainer Data

**What does this company do?**
{ov.get('about', 'No description available.')}

**Sector**: {', '.join(ov.get('sectors', [])) or '—'}
**Current Price**: {ov.get('current_price') or '— (not on source page)'}

**Key Numbers (explain each in simple language):**
"""
    for k, v in ratios.items():
        instructions += f"  - {k}: {v or '— (not on source page)'}\n"

    pl = data.get("profit_loss", {})
    if pl.get("years") and pl.get("rows"):
        years = pl["years"][-5:]
        instructions += f"\n**5 Year Revenue & Profit Trend (years: {', '.join(years)}):**\n"
        for row in pl["rows"][:5]:
            vals = row.get("values", [])[-5:]
            instructions += f"  {row['label']}: {' | '.join(vals)}\n"

    instructions += """
---
ANALYST TASK: Using the data above, explain this company to someone who has never invested before.
Use simple language, analogies, and avoid jargon. Cover:
1. What does this company do and how does it make money?
2. Is it profitable? Is it growing?
3. Is it a good business? (ROCE, ROE in simple terms)
4. How much debt does it have? (simple explanation)
5. Is the current price expensive or cheap? (PE ratio explained simply)
6. What are the 3 most interesting things about this company?
7. What should a beginner be careful about before investing?

Keep it conversational, like explaining to a friend.
---
"""
    missing = overview_missing_fields(ov)
    return ToolResult(
        data={"report": instructions},
        warnings=list(page.warnings),
        missing_fields=missing,
        reason=explain_missing(missing, len(missing) == len(OVERVIEW_CORE_FIELDS)) if missing else None,
        meta=page.meta,
    )


# ─── formatting helpers ────────────────────────────────────────────────────────

def _fmt_table(title: str, data: dict, n_years: int = 5) -> str:
    years = data.get("years", [])[-n_years:]
    rows = data.get("rows", [])
    if not years or not rows:
        return f"## {title}\nNo data available."

    lines = [f"## {title}", ""]
    year_str = "  ".join(f"{y:>12}" for y in years)
    lines.append(f"{'Metric':<35} {year_str}")
    lines.append("-" * (35 + 14 * len(years)))
    for row in rows:
        label = row.get("label", "")[:35]
        all_vals = row.get("values", [])
        values = all_vals[-n_years:]
        values = values + [""] * (len(years) - len(values))
        val_str = "  ".join(f"{v:>12}" for v in values)
        lines.append(f"{label:<35} {val_str}")
    return "\n".join(lines)


def _fmt_shareholding(data: dict) -> str:
    quarters = data.get("quarters", [])[-6:]
    rows = data.get("rows", [])
    if not quarters or not rows:
        return "## Shareholding Pattern\nNo data available."

    lines = ["## Shareholding Pattern (%)", ""]
    q_str = "  ".join(f"{q:>12}" for q in quarters)
    lines.append(f"{'Category':<25} {q_str}")
    lines.append("-" * (25 + 14 * len(quarters)))
    for row in rows:
        cat = row.get("category", "")[:25]
        all_vals = row.get("values", [])
        vals = all_vals[-len(quarters):]
        vals = vals + [""] * (len(quarters) - len(vals))
        val_str = "  ".join(f"{v:>12}" for v in vals)
        lines.append(f"{cat:<25} {val_str}")
    return "\n".join(lines)


def _fmt_peers(peers: list) -> str:
    if not peers:
        return "## Peers\nNo peer data available (peer table loads via AJAX on Screener.in)."

    # Check if we only have sector breadcrumb context (no actual peer rows)
    if peers and peers[0].get("_note"):
        lines = ["## Peer Comparison", "", peers[0]["_note"], ""]
        sector_rows = [p for p in peers[1:] if "Sector Level" in p]
        for r in sector_rows:
            lines.append(f"  {r['Sector Level']}: {r['Name']}")
        lines.append(
            "\nTo compare peers, use `compare_companies([\"SYMBOL1\", \"SYMBOL2\", ...])`."
        )
        return "\n".join(lines)

    columns = list(peers[0].keys()) if peers else []
    # Remove internal URL / note keys
    columns = [c for c in columns if not c.startswith("_")]
    if not columns:
        return "## Peers\nNo peer data available (peer table loads via AJAX on Screener.in)."

    col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in peers)) for c in columns}
    col_widths = {c: min(w, 20) for c, w in col_widths.items()}

    lines = ["## Peer Comparison", ""]
    header = "  ".join(f"{c:{col_widths[c]}}" for c in columns)
    sep = "  ".join("-" * col_widths[c] for c in columns)
    lines += [header, sep]
    for row in peers:
        lines.append("  ".join(f"{str(row.get(c, ''))[:col_widths[c]]:{col_widths[c]}}" for c in columns))
    return "\n".join(lines)
