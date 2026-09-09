# Contributing to screener-mcp

Thanks for helping improve screener-mcp. This guide covers everything you need to add tools, fix bugs, or extend the project.

---

## Local development setup

**Prerequisites:** Python 3.11+, a free [Screener.in](https://www.screener.in/register/) account.

```bash
git clone https://github.com/LogeshR15/screener-mcp
cd screener-mcp

python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Core install
pip install -e ".[dev]"

# For document analysis tools (analyze_annual_report, analyze_earnings_call)
pip install -e ".[ai]"           # adds pdfplumber, chromadb, sentence-transformers
```

Add credentials to `~/.zshrc` or `~/.bashrc`:

```bash
export SCREENER_USERNAME="your@email.com"
export SCREENER_PASSWORD="yourpassword"
```

Run the test suite:

```bash
pytest
```

These tests are offline — no network calls, no credentials needed. They check
that the tool registry and the documentation still agree, so they fail if you
add or rename a tool without updating the docs. See
[Submitting a PR](#submitting-a-pr).

Test the server runs:

```bash
python run_server.py
```

Register with Claude Code for interactive testing:

```bash
claude mcp add screener-dev -s local -- \
  $(pwd)/.venv/bin/python3.11 \
  $(pwd)/run_server.py
```

---

## Project structure

```
screener-mcp/
├── run_server.py                      # Entry point (calls server.main())
├── tests/
│   └── test_tools.py                  # Offline registry + docs-consistency tests
└── src/screener_mcp/
    ├── server.py                      # FastMCP — all 23 tool definitions (start here)
    ├── client.py                      # Screener.in HTTP client + auth
    ├── core/
    │   ├── nse_client.py              # NSE India API client (announcements, filings)
    │   ├── rag.py                     # PDF processing + semantic search pipeline
    │   └── vector_store.py            # ChromaDB wrapper for document indexing
    ├── parsers/
    │   ├── company.py                 # Parses Screener.in company HTML
    │   └── screener.py                # Parses stock screener result HTML/JSON
    └── tools/
        ├── company_tools.py           # Financials, overview, shareholding, peers
        ├── screening_tools.py         # Screen queries + 15 pre-built themes
        ├── analysis_tools.py          # Red flags, deep analysis, beginner explainer
        ├── documents.py               # Annual reports + earnings calls via RAG
        ├── announcements.py           # NSE corporate announcements
        ├── shareholders.py            # Bulk deal / shareholder search
        ├── commodities.py             # Commodity price analysis
        └── notebook.py               # Persistent research notes
```

---

## Adding a new tool

### Step 1 — Write the data-fetching function

Add your function to the most relevant file in `tools/`, or create a new file.

```python
# src/screener_mcp/tools/company_tools.py

async def get_concall_schedule(symbol: str) -> str:
    """Fetch upcoming earnings call / AGM schedule for a company."""
    client = await get_client()
    html = await client.get_html(f"/company/{symbol.upper()}/")
    # parse and return a formatted string
    ...
    return formatted_result
```

### Step 2 — Register it in server.py

```python
# server.py — add to the import block
from .tools.company_tools import get_concall_schedule as _get_concall_schedule

# server.py — add a @mcp.tool() definition
@mcp.tool()
async def get_concall_schedule(symbol: str) -> str:
    """
    Get the upcoming earnings call or AGM schedule for a company.

    symbol: NSE/BSE symbol (e.g., "TCS", "HDFCBANK")

    Example:
      get_concall_schedule("INFY")
    """
    return await _safe(_get_concall_schedule)(symbol)
```

### Step 3 — Test interactively

```
"Get the earnings call schedule for TCS"
```

### Rules

- **Always wrap with `_safe(...)`** — gives clean error messages instead of stack traces
- **Return a plain string** — markdown formatting is fine and preferred
- **Write a clear docstring** — Claude uses it to decide when and how to call your tool; include an example
- **No login for pure data tools** — if your tool needs auth, add the `_LOGIN_REQUIRED_MSG` pattern (see `screening_tools.py` for reference)
- **Keep it focused** — one tool, one job; don't add optional complexity upfront

---

## Adding a new screening theme

Open `src/screener_mcp/tools/screening_tools.py` and add to both dicts:

```python
QUERY_TEMPLATES = {
    ...
    "asset_light": (
        "Return on capital employed > 25 AND "
        "Net cash flow last year > 0 AND "
        "Debt to equity < 0.2"
    ),
}

THEME_DESCRIPTIONS = {
    ...
    "asset_light": "Capital-light businesses with high ROCE and positive free cash flow",
}
```

Done — immediately available via `screen_by_theme("asset_light")` and `list_investment_themes()`.

---

## Adding a new data source

If you need to pull from a new API (BSE, SEBI EDGAR, MCX), add a client module in `core/`:

```python
# src/screener_mcp/core/bse_client.py

import httpx

BSE_BASE = "https://api.bseindia.com"

class BSEClient:
    async def get_corporate_actions(self, bse_code: str) -> list[dict]:
        ...

_client = None

async def get_bse_client() -> BSEClient:
    global _client
    if _client is None:
        _client = BSEClient()
    return _client
```

Then import and use it in your tool file.

---

## How the RAG pipeline works

`analyze_annual_report` and `analyze_earnings_call` use a fully local RAG pipeline — no external AI API needed:

```
PDF URL
  → httpx download (cached to ~/.screener-mcp/pdf_cache/)
  → pdfplumber: extract text per page
  → chunk: 500-word segments with 60-word overlap
  → sentence-transformers: embed each chunk (all-MiniLM-L6-v2, ~80 MB, local)
  → ChromaDB: store with page metadata (cached to ~/.screener-mcp/chroma_db/)
  → query: embed question → cosine similarity → top-5 chunks
  → return excerpts for Claude to reason over
```

To extend this (e.g., support HTML transcripts, SEBI filings, or BSE documents), edit `core/rag.py`.

---

## Screener.in query field names

Exact spelling matters in `screen_stocks` queries.

| Field | Notes |
|---|---|
| `Market Capitalization` | ₹ Crore |
| `Current Price` | |
| `Price to Earning` | PE ratio |
| `Price to book value` | PB ratio |
| `EV / EBITDA` | |
| `PEG Ratio` | |
| `Return on capital employed` | % |
| `Return on equity` | % |
| `Average return on capital employed 5Years` | % |
| `Average return on equity 5Years` | % |
| `Debt to equity` | |
| `Current ratio` | |
| `Dividend yield` | % |
| `Pledged percentage` | % |
| `Net cash flow last year` | ₹ Crore |
| `Sales growth 5Years` / `3Years` / `last year` | % |
| `Profit growth 5Years` / `3Years` / `last year` | % |

Operators: `>` `<` `=` `AND`

---

## Submitting a PR

1. Fork the repo and create a branch: `git checkout -b feat/my-tool`
2. Make your changes following the rules above
3. If you added or renamed a tool, update all three: `EXPECTED_TOOLS` in
   `tests/test_tools.py`, the `server.py` module docstring, and the README
   tool table (including the `## Tools — N total` count)
4. Run `pytest` — it must pass; CI runs it on every PR
5. Test interactively with Claude Code
6. Open a PR — include:
   - What the tool does (one sentence)
   - An example query that triggers it
   - Any new dependencies and why they're needed

---

## Questions?

Open an issue at [github.com/LogeshR15/screener-mcp/issues](https://github.com/LogeshR15/screener-mcp/issues)
or reach out directly: **logeshl2003@gmail.com**
