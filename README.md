# screener-mcp — Indian Stock Research for Claude - 300+  Active users

Contact; logeshl2003@gmail.com 
inkedin.com/in/logesh-ramasamy/


Turn Claude into a personal Indian equity analyst powered by live [Screener.in](https://www.screener.in) data.

> **Ask Claude naturally** — it fetches real financial data and reasons over it like a seasoned analyst.

---

## Demo

<!-- Add a GIF here showing Claude answering "Compare TCS and Infosys" -->
> Screenshot / GIF coming soon. Try it yourself and see!

---

## What you can ask

```
"Compare ITC and HUL"
"Find chemical stocks with low debt and strong growth"
"Explain Jyothy Labs like I'm a beginner"
"What are the red flags in Asian Paints?"
"Find hidden gems below ₹5000 crore market cap"
"Show me TCS quarterly results for last 2 years"
"What is the promoter holding trend for Titan?"
"Find companies with improving ROCE over 5 years"
"Which companies benefit from EV adoption?"
"Give me businesses with 15%+ profit growth and low debt"
```

---

## Quick install (recommended)

If you have [uv](https://github.com/astral-sh/uv) installed (`pip install uv` or `brew install uv`):

```bash
claude mcp add screener -s user -- uvx screener-mcp
```

That's it. No cloning, no venv setup — `uvx` handles everything automatically.

> **Don't have uv?** Install it: `pip install uv` or `brew install uv`

---

## Manual install (alternative)

```bash
git clone https://github.com/LogeshR15/screener-mcp
cd screener-mcp

python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install httpx beautifulsoup4 lxml "mcp[cli]"

claude mcp add screener -s user -- \
  $(pwd)/.venv/bin/python3.11 \
  $(pwd)/run_server.py
```

---

## Setup credentials (required for stock screening)

Company data (financials, ratios, shareholding) works **without login**.
Stock screening requires a free Screener.in account.

**1. Register free** at [screener.in/register](https://www.screener.in/register/)

**2. Add to your shell profile** (`~/.zshrc` or `~/.bashrc`):

```bash
export SCREENER_USERNAME="your@email.com"
export SCREENER_PASSWORD="yourpassword"
```

**3. Reload your shell** (`source ~/.zshrc`) and restart Claude Code.

---

## Verify it's connected

```bash
claude mcp list
# screener  stdio  ✓ Connected
```

Then ask Claude: `"Search for Asian Paints"` — you should get results.

---

## Tools available

| Tool | What it does | Login needed? |
|------|-------------|:---:|
| `search_company` | Find company by name or symbol | No |
| `get_company_overview` | Key ratios, price, about | No |
| `get_financials` | P&L / Balance Sheet / Cash Flow / Ratios | No |
| `get_quarterly_results` | Last 8 quarters of results | No |
| `get_shareholding_pattern` | Promoter/FII/DII holding trends | No |
| `get_peer_comparison` | Peer comparison table | No |
| `compare_companies` | Side-by-side multi-company comparison | No |
| `get_full_analysis` | All data combined for deep analysis | No |
| `analyze_red_flags` | Structured red flag detection | No |
| `explain_for_beginners` | Plain-language company explainer | No |
| `screen_stocks` | Custom Screener.in query | Yes |
| `screen_by_theme` | Pre-built thematic screens | Yes |
| `list_investment_themes` | Show all available themes | No |

---

## Pre-built themes

```
undervalued_small_cap     — Small caps with ROCE > 15%, low debt
high_roce_low_debt        — ROCE > 20%, debt to equity < 0.3
compounders               — 15%+ growth: revenue, profit, ROE, ROCE
turnaround                — Strong recent profit recovery
rising_profit_falling_price — Improving profits, compressed PE
hidden_gems               — Small cap, high ROCE, strong growth
dividend_aristocrats      — Consistent dividends with quality financials
qarp                      — Quality at reasonable price
micro_cap_growth          — High-growth micro caps < ₹1000 Cr
ev_theme                  — EV & auto ancillary growth companies
chemicals                 — Specialty chemicals with strong fundamentals
defense                   — Defense sector with revenue momentum
railways                  — Railway infra/equipment companies
renewable_energy          — Renewable energy sector
```

---

## Architecture

```
screener-mcp/
├── run_server.py                  # Entry point
└── src/screener_mcp/
    ├── server.py                  # FastMCP server — tool definitions
    ├── client.py                  # HTTP client (auth, rate limiting)
    ├── parsers/
    │   ├── company.py             # Parse company HTML pages
    │   └── screener.py            # Parse screener results
    └── tools/
        ├── company_tools.py       # Company data tools
        ├── screening_tools.py     # Stock screening tools
        └── analysis_tools.py     # Deep analysis tools
```

---

## Data source & limitations

All data comes from [Screener.in](https://www.screener.in) — India's most popular stock research platform covering BSE and NSE listed companies with 10+ years of financial history.

- Financial data is updated quarterly (lags by ~1 quarter)
- This is a research tool — not financial advice
- Always verify critical data directly on Screener.in

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — adding a new tool takes ~10 minutes.

## contact

Mail: logeshl2003@gmail.com
Linkedin: inkedin.com/in/logesh-ramasamy/
