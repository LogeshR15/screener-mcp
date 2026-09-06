# screener-mcp — Indian Stock Research for Claude

> Turn Claude into a personal Indian equity analyst powered by live [Screener.in](https://www.screener.in) data — now with AI document analysis, NSE announcements, and research notebooks.

**300+ active users** · [Report an issue](https://github.com/LogeshR15/screener-mcp/issues) · [LinkedIn](https://linkedin.com/in/logesh-ramasamy/) · logeshl2003@gmail.com

---

## What you can ask

```
"Compare ITC and HUL on all key ratios"
"Find chemical stocks with low debt and strong growth"
"Explain Jyothy Labs like I'm a beginner"
"What are the red flags in Asian Paints?"
"Find hidden gems below ₹5000 crore market cap"
"What did TCS management say about margins in Q3FY25?"
"Summarize the key risks from Reliance's 2024 annual report"
"How has ITC's capex strategy evolved over the last 3 years?"
"Which of TATASTEEL, JSWSTEEL, and SAIL mentioned raw material cost pressure recently?"
"Show me recent dividend announcements for HDFCBANK"
"How does copper price affect Havells and Polycab?"
"Save a research note on TITAN — strong Q3, watch margins"
```

---

## Quick install

```bash
claude mcp add screener -s user -- uvx screener-mcp
```

> Requires [uv](https://github.com/astral-sh/uv): `pip install uv` or `brew install uv`

**Manual install:**

```bash
git clone https://github.com/LogeshR15/screener-mcp
cd screener-mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

claude mcp add screener -s user -- \
  $(pwd)/.venv/bin/python3.11 \
  $(pwd)/run_server.py
```

> No `python3.11` on your machine? Any Python **3.11+** works (e.g. `python3.12 -m venv .venv`) — just point `claude mcp add` at that same interpreter.
> `pip install -e .` fails with "editable mode currently requires a setuptools-based build"? Your venv's `pip` is too old — run `pip install --upgrade pip` first (as above), then retry.

---

## Credentials setup

Company financials work **without login**. Stock screening requires a free account.

**1.** Register free at [screener.in/register](https://www.screener.in/register/)

**2.** Add to `~/.zshrc` or `~/.bashrc`:

```bash
export SCREENER_USERNAME="your@email.com"
export SCREENER_PASSWORD="yourpassword"
```

**3.** Reload shell (`source ~/.zshrc`) and restart Claude Code.

**For document analysis** (annual reports, earnings calls), install extra deps:

```bash
pip install pdfplumber sentence-transformers chromadb
# or: pip install -e ".[ai]"
```

---

## Verify connection

```bash
claude mcp list
# screener  stdio  Connected
```

Then ask Claude: `"Search for Asian Paints"` — you should get results.

---

## Running as a remote server (HTTP)

By default this runs over stdio — a local process, used by `claude mcp add`, Claude
Desktop, and similar clients. Some integrations (e.g. Zia Agents custom tools,
or any client that asks for an HTTPS **Server URL**) instead need a network server.

Run it with HTTP transport:

```bash
MCP_TRANSPORT=streamable-http PORT=8000 python run_server.py
# Serves MCP over HTTP at http://<host>:8000/mcp
```

Env vars:
- `MCP_TRANSPORT` — `stdio` (default) or `streamable-http`
- `PORT` / `MCP_PORT` — port to listen on (default `8000`)
- `MCP_HOST` — bind address (default `0.0.0.0`)

To get a public HTTPS URL, deploy this to any host that can run a long-lived
Python process and terminate TLS for you (Render, Railway, Fly.io, a VM behind
a reverse proxy, etc.), then point the client at `https://your-host/mcp`.

> Note: a bare `streamable-http` server has no authentication. If you deploy
> it publicly, put it behind your platform's access controls (API gateway,
> IP allowlist, auth proxy) rather than exposing it to the open internet
> unauthenticated — especially if you set `SCREENER_USERNAME`/`PASSWORD`,
> since anyone who can reach the URL would act as your Screener.in account.

---

## Tools — 23 total

### Company Research

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `search_company` | Find company by name or symbol | No |
| `get_company_overview` | Key ratios, price, 52W range, about | No |
| `get_financials` | P&L / Balance Sheet / Cash Flow / Ratios | No |
| `get_quarterly_results` | Last 8 quarters of results | No |
| `get_shareholding_pattern` | Promoter / FII / DII holding trend | No |
| `get_peer_comparison` | Sector peer comparison table | No |
| `compare_companies` | Side-by-side comparison (2–5 stocks) | No |
| `compare_stocks_ui` | Interactive dashboard (Claude Desktop) | No |
| `get_full_analysis` | All data combined for deep analysis | No |
| `analyze_red_flags` | Structured red flag detection | No |
| `explain_for_beginners` | Plain-language company explainer | No |

### Stock Screening

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `screen_stocks` | Custom Screener.in query | Yes |
| `screen_by_theme` | Pre-built thematic screens | Yes |
| `list_investment_themes` | Show all available themes | No |

### Document Analysis *(new)*

| Tool | What it does | Extra deps needed |
|------|-------------|:---:|
| `get_document_list` | List annual reports & earnings call transcripts | No |
| `analyze_annual_report` | Ask any question over one annual report PDF | Yes |
| `analyze_earnings_call` | Ask any question over one earnings call transcript | Yes |
| `ask_company_research` | Ask a question across ALL of a company's cached documents at once (multiple years/quarters) | Yes |
| `search_market_commentary` | Search a question across multiple companies' already-indexed documents at once | Yes |

> Uses a local RAG pipeline: PDF → pdfplumber → ChromaDB → sentence-transformers. Results are cached on disk — the same report is never re-downloaded or re-indexed.
> `ask_company_research` and `search_market_commentary` build on the same cache — the former indexes a company's recent documents and searches across them together (good for "how has X changed over time?"); the latter searches only what's *already* indexed across several symbols (good for "which of these companies mentioned Y?").

### Corporate Events *(new)*

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `get_company_announcements` | NSE corporate announcements with category filter | No |
| `search_shareholder` | Find investor activity via NSE bulk deals | No |

### Market & Research *(new)*

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `get_commodity_prices` | Commodity price context + impacted companies | No |
| `notebook_ai` | Save, read, and AI-summarize research notes locally | No |

---

## Pre-built screening themes

```
undervalued_small_cap       Small caps, ROCE > 15%, low debt, PE < 20
high_roce_low_debt          ROCE > 20%, debt to equity < 0.3
compounders                 15%+ growth: revenue, profit, ROE, ROCE
turnaround                  Strong recent profit recovery
rising_profit_falling_price Improving profits, compressed valuation
improving_roce              ROCE > 15% with profit momentum
hidden_gems                 Small cap, high ROCE, strong growth
dividend_aristocrats        Consistent dividends with quality financials
qarp                        Quality at reasonable price
micro_cap_growth            High-growth micro caps < ₹1000 Cr
ev_theme                    EV & auto ancillary growth companies
chemicals                   Specialty chemicals, strong fundamentals
defense                     Defense sector with revenue momentum
railways                    Railway infra/equipment companies
renewable_energy            Renewable energy sector
```

---

## Custom screen syntax

```
Market Capitalization < 5000 AND Return on capital employed > 15 AND Debt to equity < 0.5
Profit growth 5Years > 20 AND Sales growth 5Years > 15 AND Debt to equity < 0.3
Dividend yield > 3 AND Return on equity > 15 AND Pledged percentage < 5
```

Supported operators: `>` `<` `=` `AND`

Full field list in [CONTRIBUTING.md](CONTRIBUTING.md#screenerinscreenerinquery-field-names).

---

## How document analysis works

```
analyze_annual_report("TCS", 2024, "What are the key risks?")

  1. Fetch PDF link from Screener.in / NSE
  2. Download and parse with pdfplumber
  3. Chunk into 500-word overlapping segments
  4. Embed with sentence-transformers (runs locally, no API key needed)
  5. Store in ChromaDB (~/.screener-mcp/chroma_db/)
  6. Semantic search returns top-5 relevant excerpts
  7. Claude reasons over the excerpts to answer your question

Results are cached — the same report is never re-processed twice.
```

---

## Architecture

```
screener-mcp/
├── run_server.py
└── src/screener_mcp/
    ├── server.py                   # FastMCP — all 21 tool definitions
    ├── client.py                   # Screener.in HTTP client + auth
    ├── core/
    │   ├── nse_client.py           # NSE India API (announcements, filings)
    │   ├── rag.py                  # PDF → chunk → embed → query pipeline
    │   └── vector_store.py         # ChromaDB wrapper
    ├── parsers/
    │   ├── company.py              # Screener.in company page parser
    │   └── screener.py             # Screen results parser
    └── tools/
        ├── company_tools.py        # Company data tools
        ├── screening_tools.py      # Stock screening + themes
        ├── analysis_tools.py       # Deep analysis, red flags, beginner
        ├── documents.py            # Annual reports + earnings calls (RAG)
        ├── announcements.py        # NSE corporate announcements
        ├── shareholders.py         # Bulk deal / shareholder search
        ├── commodities.py          # Commodity price analysis
        └── notebook.py             # Research notes
```

---

## Data sources & limitations

| Source | Data provided |
|--------|--------------|
| [Screener.in](https://www.screener.in) | 10+ years of financials, ratios, shareholding, peers |
| [NSE India](https://www.nseindia.com) | Announcements, annual reports, bulk deals |
| [MCX India](https://www.mcxindia.com) | Commodity prices (best-effort) |

- Financial data lags by ~1 quarter
- Document analysis requires machine-readable PDFs (scanned/image-only PDFs may fail)
- NSE bulk deals only capture single trades > 0.5% of equity
- `get_company_announcements` and `search_shareholder` depend on NSE's public API, which
  frequently rate-limits or blocks server IPs (403/404 responses) — if a query returns
  "no data found", it may be NSE blocking the request rather than an empty result
- This is a research tool — not financial advice

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — adding a new tool takes ~10 minutes.

---

## Contact

**Logesh Ramasamy** · logeshl2003@gmail.com · [LinkedIn](https://linkedin.com/in/logesh-ramasamy/)
