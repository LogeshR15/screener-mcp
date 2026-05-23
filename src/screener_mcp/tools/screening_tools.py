"""
Stock screening tools — translate natural-language intent into Screener.in queries
and return formatted results with analyst commentary.
"""

from typing import Optional
from urllib.parse import quote_plus

from ..client import get_client
from ..parsers.screener import parse_screen_results

# ─── Pre-built query templates ─────────────────────────────────────────────────
# These map natural language themes to Screener query strings.

QUERY_TEMPLATES = {
    # Quality & Value
    "undervalued_small_cap": (
        "Market Capitalization < 5000 AND "
        "Return on capital employed > 15 AND "
        "Debt to equity < 0.5 AND "
        "Profit growth 3Years > 10 AND "
        "Price to Earning < 20"
    ),
    "high_roce_low_debt": (
        "Return on capital employed > 20 AND "
        "Debt to equity < 0.3 AND "
        "Profit growth 5Years > 12"
    ),
    "compounders": (
        "Sales growth 5Years > 15 AND "
        "Profit growth 5Years > 15 AND "
        "Return on equity > 15 AND "
        "Debt to equity < 0.5 AND "
        "Return on capital employed > 15"
    ),
    "turnaround": (
        "Profit growth 3Years > 25 AND "
        "Profit growth last year > 20 AND "
        "Sales growth 3Years > 10 AND "
        "Return on capital employed > 10"
    ),
    "rising_profit_falling_price": (
        "Profit growth 3Years > 15 AND "
        "Sales growth 3Years > 10 AND "
        "Price to Earning < 15"
    ),
    "improving_roce": (
        "Return on capital employed > 15 AND "
        "Profit growth 5Years > 12 AND "
        "Debt to equity < 1"
    ),
    "hidden_gems": (
        "Market Capitalization < 5000 AND "
        "Return on capital employed > 15 AND "
        "Sales growth 5Years > 15 AND "
        "Debt to equity < 0.5"
    ),
    "dividend_aristocrats": (
        "Dividend yield > 2 AND "
        "Profit growth 5Years > 8 AND "
        "Return on equity > 12 AND "
        "Debt to equity < 0.5"
    ),

    # Sector themes
    "ev_theme": (
        "Sales growth 3Years > 15 AND "
        "Debt to equity < 1"
        # User should filter by sector manually; Screener doesn't have EV-tag filter
    ),
    "chemicals": (
        "Debt to equity < 0.5 AND "
        "Profit growth 5Years > 15 AND "
        "Return on capital employed > 15 AND "
        "Sales growth 5Years > 12"
    ),
    "defense": (
        "Sales growth 3Years > 15 AND "
        "Return on capital employed > 12"
    ),
    "railways": (
        "Sales growth 3Years > 15 AND "
        "Profit growth 3Years > 20 AND "
        "Debt to equity < 1"
    ),
    "renewable_energy": (
        "Sales growth 3Years > 15 AND "
        "Debt to equity < 2"
    ),

    # Quality at reasonable price
    "qarp": (
        "Price to Earning < 25 AND "
        "Return on equity > 15 AND "
        "Profit growth 5Years > 12 AND "
        "Debt to equity < 0.5 AND "
        "Market Capitalization > 1000"
    ),

    # Micro caps with momentum
    "micro_cap_growth": (
        "Market Capitalization < 1000 AND "
        "Sales growth 3Years > 20 AND "
        "Profit growth 3Years > 20 AND "
        "Return on capital employed > 15"
    ),
}

THEME_DESCRIPTIONS = {
    "ev_theme": "EV & Auto ancillary companies with strong growth",
    "chemicals": "Specialty chemicals with low debt and strong growth",
    "defense": "Defense sector with revenue momentum",
    "railways": "Railway infra/equipment with profit growth",
    "renewable_energy": "Renewable energy companies with revenue growth",
    "undervalued_small_cap": "Small caps (< ₹5000 Cr) with high ROCE, low debt",
    "high_roce_low_debt": "High ROCE (>20%) companies with minimal debt",
    "compounders": "Classic compounders: 15%+ growth on all fronts",
    "turnaround": "Turnaround stories with strong recent recovery",
    "rising_profit_falling_price": "Improving profits with low PE (potential value)",
    "improving_roce": "Companies with ROCE >15% and profit momentum",
    "hidden_gems": "Hidden gems: small cap, high ROCE, strong growth",
    "dividend_aristocrats": "Consistent dividend payers with quality financials",
    "qarp": "Quality at reasonable price (QARP)",
    "micro_cap_growth": "High-growth micro caps (< ₹1000 Cr)",
}


_LOGIN_REQUIRED_MSG = """
**Login required for stock screening.**

Stock screening on Screener.in requires a free account. To enable it:

1. Register for free at https://www.screener.in/register/
2. Set these environment variables before starting the MCP server:
   ```
   SCREENER_USERNAME=your@email.com
   SCREENER_PASSWORD=yourpassword
   ```
3. Restart the MCP server.

**Query you tried**: `{query}`

**What you can do now (no login needed)**:
- `search_company("name")` — search for any company
- `get_company_overview("SYMBOL")` — full key ratios
- `get_financials("SYMBOL")` — P&L, balance sheet, cash flow
- `get_quarterly_results("SYMBOL")` — last 8 quarters
- `get_shareholding_pattern("SYMBOL")` — promoter/FII/DII trends
- `compare_companies(["ITC", "HUL"])` — side-by-side comparison
"""


async def screen_stocks(
    query: str,
    sort_by: str = "",
    order: str = "desc",
    limit: int = 25,
) -> str:
    """
    Run a custom Screener.in query.

    Example queries:
      "Market Capitalization < 5000 AND Return on capital employed > 15"
      "Debt to equity < 0.5 AND Profit growth 5Years > 15"
    """
    client = await get_client()
    try:
        html = await client.get_html(
            "/screen/raw/",
            params={"query": query, "sort": sort_by, "order": order},
        )
    except PermissionError:
        return _LOGIN_REQUIRED_MSG.format(query=query)
    data = parse_screen_results(html)

    companies = data.get("companies", [])[:limit]
    if not companies:
        return (
            f"No companies found matching the screen query.\n\n"
            f"**Query used**: `{query}`\n\n"
            "Tip: Screener query syntax uses field names like:\n"
            "  `Market Capitalization`, `Return on capital employed`,\n"
            "  `Debt to equity`, `Profit growth 5Years`, `Price to Earning`"
        )

    count_text = data.get("count", str(len(companies)))
    columns = data.get("columns", [])

    lines = [
        f"## Screener Results",
        f"**Query**: `{query}`",
        f"**Total matches**: {count_text}  |  Showing top {len(companies)}",
        "",
    ]

    # Format as table
    if columns and companies:
        col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in companies)) for c in columns}
        header = "  ".join(f"{c:{col_widths[c]}}" for c in columns)
        sep = "  ".join("-" * col_widths[c] for c in columns)
        lines += [header, sep]
        for r in companies:
            lines.append("  ".join(f"{str(r.get(c, '')):{col_widths[c]}}" for c in columns))

    return "\n".join(lines)


async def screen_by_theme(theme: str, limit: int = 20) -> str:
    """
    Run a pre-built thematic screen.

    Available themes:
      undervalued_small_cap, high_roce_low_debt, compounders, turnaround,
      rising_profit_falling_price, improving_roce, hidden_gems,
      dividend_aristocrats, qarp, micro_cap_growth,
      ev_theme, chemicals, defense, railways, renewable_energy
    """
    # Fuzzy match theme
    theme_key = _match_theme(theme)
    if not theme_key:
        available = "\n".join(f"  - `{k}`: {v}" for k, v in THEME_DESCRIPTIONS.items())
        return f"Theme '{theme}' not recognized.\n\nAvailable themes:\n{available}"

    query = QUERY_TEMPLATES[theme_key]
    description = THEME_DESCRIPTIONS[theme_key]

    header = [
        f"## {description}",
        f"**Theme**: `{theme_key}`",
        f"**Filter**: `{query}`",
        "",
    ]
    result = await screen_stocks(query, limit=limit)
    # Strip the first "## Screener Results" line from result to avoid duplication
    result_lines = result.split("\n")
    if result_lines and result_lines[0].startswith("## Screener"):
        result_lines = result_lines[1:]
    return "\n".join(header + result_lines)


async def list_themes() -> str:
    """List all available pre-built screening themes."""
    lines = ["## Available Investment Themes", ""]
    for key, desc in THEME_DESCRIPTIONS.items():
        q = QUERY_TEMPLATES[key]
        lines.append(f"### `{key}`")
        lines.append(f"{desc}")
        lines.append(f"```\n{q}\n```")
        lines.append("")
    return "\n".join(lines)


def _match_theme(theme: str) -> Optional[str]:
    """Fuzzy match a theme name to a template key."""
    theme_lower = theme.lower().replace(" ", "_").replace("-", "_")
    if theme_lower in QUERY_TEMPLATES:
        return theme_lower
    for key in QUERY_TEMPLATES:
        if theme_lower in key or key in theme_lower:
            return key
    # Partial word match
    words = set(theme_lower.split("_"))
    for key in QUERY_TEMPLATES:
        key_words = set(key.split("_"))
        if words & key_words:
            return key
    return None
