# Screener.in MCP Server — Indian Stock Research Assistant

Turn Claude into a personal Indian equity analyst. Ask natural questions about Indian stocks and get research-grade answers backed by live Screener.in data.

## What you can ask Claude

```
"Compare ITC and HUL"
"Find chemical stocks with low debt and strong growth"
"Explain Jyothy Labs like I'm a beginner"
"What are the red flags in this company?"
"Find hidden gems below ₹5000 crore market cap"
"Show me TCS quarterly results for last 2 years"
"What is the promoter holding trend for Titan?"
"Find companies with improving ROCE over 5 years"
"Which companies benefit from EV adoption?"
"Find turnaround stories in mid-cap space"
"Give me businesses with 15%+ profit growth and low debt"
```

## Setup

### 1. Prerequisites

- Python 3.11+ (via Homebrew: `brew install python@3.11`)
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
- A free Screener.in account (for screening features)

### 2. Install

```bash
# Clone / copy this project
cd /path/to/screener-mcp

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install httpx beautifulsoup4 lxml "mcp[cli]"
```

### 3. Configure credentials (required for screening)

Company data (overview, financials, shareholding) works **without login**.
Stock screening requires a free Screener.in account.

```bash
# In your shell profile (.zshrc / .bashrc):
export SCREENER_USERNAME="your@email.com"
export SCREENER_PASSWORD="yourpassword"
```

Register free at: https://www.screener.in/register/

### 4. Register with Claude Code

```bash
claude mcp add screener -s user -- \
  /path/to/screener-mcp/.venv/bin/python3.11 \
  /path/to/screener-mcp/run_server.py
```

Verify:
```bash
claude mcp list
# screener: ... ✓ Connected
```

### 5. Use in Claude

Open Claude Code and start asking:

```
"Search for Asian Paints"
"Compare INFY and TCS"
"Find hidden gems with high ROCE"
"Explain HDFC Bank for a beginner"
```

---

## Tools available

| Tool | What it does | Login needed? |
|------|-------------|---------------|
| `search_company` | Find company by name or symbol | No |
| `get_company_overview` | Key ratios, price, about | No |
| `get_financials` | P&L / Balance Sheet / Cash Flow | No |
| `get_quarterly_results` | Last 8 quarters of results | No |
| `get_shareholding_pattern` | Promoter/FII/DII holding trends | No |
| `get_peer_comparison` | Peer comparison table | No |
| `compare_companies` | Side-by-side multi-company comparison | No |
| `get_full_analysis` | All data combined for deep analysis | No |
| `analyze_red_flags` | Structured red flag detection | No |
| `explain_for_beginners` | Plain-language company explainer | No |
| `screen_stocks` | Custom Screener.in query | **Yes** |
| `screen_by_theme` | Pre-built thematic screens | **Yes** |
| `list_investment_themes` | Show available themes | No |

## Pre-built themes (require login)

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

## Data source

All data comes from [Screener.in](https://www.screener.in) — India's most popular stock research platform. Data covers BSE and NSE listed companies with 10+ years of financial history.

**Limitations:**
- Financial data is updated quarterly (lags by ~1 quarter)
- Screener.in data is for research only, not financial advice
- Past performance does not guarantee future results
- Always verify critical data directly on Screener.in

## Architecture

```
screener-mcp/
├── src/screener_mcp/
│   ├── server.py          # FastMCP server — tool definitions
│   ├── client.py          # HTTP client (auth, rate limiting)
│   ├── parsers/
│   │   ├── company.py     # Parse company HTML pages
│   │   └── screener.py    # Parse screener results
│   └── tools/
│       ├── company_tools.py    # Company data tools
│       ├── screening_tools.py  # Stock screening tools
│       └── analysis_tools.py   # Deep analysis tools
└── run_server.py          # Entry point
```
