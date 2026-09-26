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

# For document analysis tools (ask_company_research, search_market_commentary)
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
│   ├── test_tools.py                  # Offline registry + docs-consistency tests
│   ├── test_data_quality.py           # Envelope, resolution, ratio flags, technicals, cache, NSE
│   └── fixtures/                      # Trimmed real Screener pages
├── scripts/canary.py                  # Live checks against Screener.in (runs daily in CI)
└── src/screener_mcp/
    ├── server.py                      # FastMCP — all 30 tool definitions (start here)
    ├── client.py                      # Screener.in HTTP client + auth
    ├── core/
    │   ├── envelope.py                # Standard response envelope (ToolResult, ToolError)
    │   ├── company_page.py            # Symbol resolution + page fetch + standalone fallback
    │   ├── quality.py                 # Missing-field detection + ratio sanity bounds
    │   ├── technicals.py              # Price history → 52W range, DMA, RSI, volume ratio
    │   ├── indices.py                 # NSE index universes + sector benchmarks
    │   ├── nse_client.py              # NSE India API client (announcements, filings)
    │   ├── history.py                 # Year-by-year series, computed ROE / debt-to-equity, CAGR
    │   ├── valuation_history.py       # Year-end P/E and P/B from Screener's chart API
    │   ├── rag.py                     # PDF processing + semantic search pipeline
    │   └── vector_store.py            # ChromaDB wrapper for document indexing
    ├── parsers/
    │   ├── company.py                 # Parses Screener.in company HTML
    │   └── screener.py                # Parses stock screener result HTML/JSON
    └── tools/
        ├── company_tools.py           # Financials, overview, shareholding, peers
        ├── screening_tools.py         # Screen queries + 15 pre-built themes
        ├── technical_tools.py         # Technical screens, 52W-low candidates, sector-relative
        ├── analysis_tools.py          # Full analysis, rule-based red flags
        ├── documents.py               # Annual reports + earnings calls via RAG
        ├── announcements.py           # NSE corporate announcements (incl. credit/ESG ratings)
        ├── shareholders.py            # NSE bulk deals (by company and/or investor)
        ├── commodities.py             # Commodity price analysis
        └── notebook.py               # Persistent research notes
```

---

## Adding a new tool

### Step 1 — Write the data-fetching function

Add your function to the most relevant file in `tools/`, or create a new file.

```python
# src/screener_mcp/tools/company_tools.py

from ..core.company_page import fetch_company_page
from ..core.envelope import ToolResult

async def get_concall_schedule(symbol: str) -> ToolResult:
    """Fetch upcoming earnings call / AGM schedule for a company."""
    page = await fetch_company_page(symbol)   # resolves symbols, handles standalone fallback
    schedule = parse_schedule(page.html)
    if not schedule:
        return ToolResult(data={"events": []}, warnings=page.warnings, meta=page.meta,
                          missing_fields=["events"], reason="No schedule on the source page.")
    return ToolResult(data={"events": schedule}, warnings=page.warnings, meta=page.meta)
```

### Step 2 — Register it in server.py

```python
# server.py — add to the import block
from .tools.company_tools import get_concall_schedule as _get_concall_schedule

# server.py — add a @mcp.tool() definition
@mcp.tool()
async def get_concall_schedule(symbol: str) -> dict:
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

- **Always wrap with `_safe(...)`**. It turns every return value into the standard envelope (`status` / `partial` / `warnings` / `data`) and every exception into a structured error
- **Return a `ToolResult`** (or a dict, or a markdown string for report-style tools). Never return blanks that look like real values. Use `None`, list the field in `missing_fields`, and give a `reason`
- **Raise `ToolError`** for expected failures (bad input, not found). Put anything the caller can act on in its keyword details, such as `candidates=[...]`
- **Fetch company pages through `fetch_company_page`** so symbol resolution and the consolidated → standalone fallback apply everywhere
- **If your tool parses a new part of a Screener page, add a check to `scripts/canary.py`** so a future layout change fails loudly instead of returning blanks
- **Write a clear docstring** — Claude uses it to decide when and how to call your tool; include an example
- **No login for pure data tools** — if your tool needs auth, add the `_LOGIN_REQUIRED_MSG` pattern (see `screening_tools.py` for reference)
- **Keep it focused** — one tool, one job; don't add optional complexity upfront

---

## Adding a new screening theme

Open `src/screener_mcp/tools/screening_tools.py` and add an entry to `THEMES`.
A theme is either a Screener query run across the market:

```python
THEMES = {
    ...
    "asset_light": {
        "description": "Capital-light businesses with high ROCE and positive free cash flow",
        "query": "Return on capital employed > 25 AND Net cash flow last year > 0 AND Debt to equity < 0.2",
    },
}
```

or, for a sector, a universe of the companies actually in it plus filters.
Screener's query language has no industry field, so a sector can't be a
query. Universe sources are `("index", <key in core/indices.py>)`,
`("industry", "/market/…/")` (the Industry link on any company page) and
`("symbols", [...])` for a curated list. Filters may only use columns those
pages carry — see the comment above `THEMES`:

```python
    "cement": {
        "description": "Cement makers with ROE > 12%",
        "universe": [("industry", "/market/IN01/IN0102/IN010203/IN010203001/")],  # Cement & Cement Products
        "filters": "Market Capitalization > 1000 AND Return on equity > 12",
    },
```

Done — immediately available via `screen_by_theme("asset_light")`, and the
theme's criteria appear in `screen_by_theme`'s tool description
automatically. Add it to the README theme list too (a test checks).

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

`ask_company_research` (and `search_market_commentary`, `get_forward_outlook`) use a fully local RAG pipeline — no external AI API needed:

```
PDF URL
  → httpx download (cached to ~/.screener-mcp/pdf_cache/)
  → pdfplumber: extract text per page (split two-column pages at the gutter,
    drop rotated/mirrored glyphs)
  → chunk: 500-word segments with 60-word overlap
  → sentence-transformers: embed each chunk (all-MiniLM-L6-v2, ~80 MB, local)
  → ChromaDB: store with page metadata (cached to ~/.screener-mcp/chroma_db/)
  → query: embed question → nearest neighbours → rerank (keyword boost,
    BRSR penalty, one hit per ~3-page window)
  → return ~900-character excerpts for Claude to reason over

If you change extraction or chunking, bump `INDEX_VERSION` in `core/rag.py`
so existing caches are rebuilt.
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

## Releasing

Publishing to PyPI happens automatically when a GitHub release is published
([`publish.yml`](.github/workflows/publish.yml)). The workflow runs the tests,
builds the package and uploads it.

1. Bump `version` in `pyproject.toml` and date the entry in `CHANGELOG.md`. The
   workflow fails if the release tag doesn't match the version, and PyPI never
   accepts the same version twice.
2. Merge to `main`, then run `gh release create vX.Y.Z --notes-file <changelog section>`.

**PyPI authentication** can be set up either way:
- **Trusted Publishing (preferred, no token):** on PyPI, open *Manage project →
  Publishing* and add a GitHub publisher (owner `LogeshR15`, repository
  `screener-mcp`, workflow `publish.yml`, no environment). Then remove the
  `PYPI_API_TOKEN` secret, since an empty secret still overrides trusted
  publishing.
- **API token:** create a project-scoped token on PyPI and store it with
  `gh secret set PYPI_API_TOKEN` **from a real terminal**. Run without a terminal,
  for example through Claude Code's `!` prefix, it can't prompt you and saves an
  empty value.

If the upload step fails, fix the authentication and re-publish the same tag:
delete the release and tag, then recreate them. Nothing reached PyPI, so no
version was used up.

---

## Questions?

Open an issue at [github.com/LogeshR15/screener-mcp/issues](https://github.com/LogeshR15/screener-mcp/issues)
or reach out directly: **logeshl2003@gmail.com**
