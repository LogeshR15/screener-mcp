"""
Higher-level analysis tools — these fetch and combine multiple data sources
to produce analyst-grade structured output for Claude to reason over.
"""

import asyncio
from typing import Optional

from ..client import get_client
from ..parsers.company import parse_full_page


async def get_full_analysis(
    symbol: str,
    financial_type: str = "consolidated",
) -> str:
    """
    Fetch ALL financial data for a company in one call.

    Returns a structured text dump that Claude can reason over to:
      - Summarize for beginners
      - Identify red flags
      - Find improving trends
      - Answer specific questions

    This is the primary tool for deep-dive analysis.
    """
    client = await get_client()
    path = f"/company/{symbol.upper()}/"
    if financial_type == "consolidated":
        path += "consolidated/"
    html = await client.get_html(path)
    data = parse_full_page(html)

    sections = []

    # ── Overview ─────────────────────────────────────────────────────────────
    ov = data.get("overview", {})
    sections.append(f"# {ov.get('name', symbol.upper())} — Full Analysis Data [{financial_type}]")
    sections.append(f"Symbol: {symbol.upper()}")
    sections.append(f"Sectors: {', '.join(ov.get('sectors', [])) or '—'}")
    sections.append(f"Current Price: {ov.get('current_price', '—')}")
    sections.append(f"52W High: {ov.get('52_week_high', '—')} | 52W Low: {ov.get('52_week_low', '—')}")
    sections.append("")
    sections.append("## Key Ratios")
    for k, v in ov.get("key_ratios", {}).items():
        sections.append(f"  {k}: {v}")
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

    # ── Shareholding ──────────────────────────────────────────────────────────
    sh = data.get("shareholding", {})
    sections.append("")
    sections.append(_fmt_shareholding(sh))

    # ── Peers ─────────────────────────────────────────────────────────────────
    peers = data.get("peers", [])
    sections.append("")
    sections.append(_fmt_peers(peers))

    return "\n".join(sections)


async def get_red_flags(symbol: str, financial_type: str = "consolidated") -> str:
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
    full_data = await get_full_analysis(symbol, financial_type)

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
    return full_data + "\n" + instructions


async def beginner_explainer(symbol: str) -> str:
    """
    Fetch company data and prepare it for a beginner-friendly explanation.
    Claude will translate numbers into plain language.
    """
    client = await get_client()
    path = f"/company/{symbol.upper()}/consolidated/"
    html = await client.get_html(path)
    data = parse_full_page(html)

    ov = data.get("overview", {})
    ratios = ov.get("key_ratios", {})

    instructions = f"""
## {ov.get('name', symbol)} — Beginner Explainer Data

**What does this company do?**
{ov.get('about', 'No description available.')}

**Sector**: {', '.join(ov.get('sectors', [])) or '—'}
**Current Price**: {ov.get('current_price', '—')}

**Key Numbers (explain each in simple language):**
"""
    for k, v in ratios.items():
        instructions += f"  - {k}: {v}\n"

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
    return instructions


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
        return "## Peers\nNo peer data available."
    columns = list(peers[0].keys()) if peers else []
    # Remove internal URL key
    columns = [c for c in columns if not c.startswith("_")]
    if not columns:
        return "## Peers\nNo peer data available."

    col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in peers)) for c in columns}
    col_widths = {c: min(w, 20) for c, w in col_widths.items()}

    lines = ["## Peer Comparison", ""]
    header = "  ".join(f"{c:{col_widths[c]}}" for c in columns)
    sep = "  ".join("-" * col_widths[c] for c in columns)
    lines += [header, sep]
    for row in peers:
        lines.append("  ".join(f"{str(row.get(c, ''))[:col_widths[c]]:{col_widths[c]}}" for c in columns))
    return "\n".join(lines)
