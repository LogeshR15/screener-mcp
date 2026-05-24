# Contributing to screener-mcp

## Setup for local development

**Prerequisites**: Python 3.11+, a free [Screener.in](https://www.screener.in/register/) account.

```bash
git clone https://github.com/LogeshR15/screener-mcp
cd screener-mcp

python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Add credentials to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export SCREENER_USERNAME="your@email.com"
export SCREENER_PASSWORD="yourpassword"
```

Run the server directly to test it:

```bash
python run_server.py
```

Or register it with Claude Code for interactive testing:

```bash
claude mcp add screener-dev -s local -- \
  $(pwd)/.venv/bin/python3.11 \
  $(pwd)/run_server.py
```

---

## Project structure

```
screener-mcp/
├── run_server.py                      # Entry point
└── src/screener_mcp/
    ├── server.py                      # FastMCP tool definitions (start here)
    ├── client.py                      # HTTP client + auth for Screener.in
    ├── parsers/
    │   ├── company.py                 # Parses company HTML pages
    │   └── screener.py                # Parses stock screener results
    └── tools/
        ├── company_tools.py           # Company data tools
        ├── screening_tools.py         # Stock screening tools
        └── analysis_tools.py         # Deep analysis / red flags
```

---

## Adding a new tool

**Step 1** — Add the data-fetching function to the relevant file in `tools/`.

```python
# src/screener_mcp/tools/company_tools.py

async def get_news(symbol: str) -> str:
    client = await get_client()
    html = await client.get_html(f"/company/{symbol.upper()}/")
    # parse and return formatted string
    ...
```

**Step 2** — Import and expose it in `server.py`:

```python
# server.py — add to the import block
from .tools.company_tools import get_news as _get_news

# server.py — add a new @mcp.tool()
@mcp.tool()
async def get_news(symbol: str) -> str:
    """
    Get recent news and announcements for a company.
    symbol: NSE/BSE symbol (e.g. "TCS", "INFY")
    """
    return await _safe(_get_news)(symbol)
```

**Rules:**
- Always wrap the call with `_safe(...)` — this gives users clean error messages instead of stack traces
- Return a plain string (markdown is fine)
- Write a clear docstring — Claude uses it to decide when to call your tool
- No login should be required for pure company data tools; add a `_LOGIN_REQUIRED_MSG` return (see `screening_tools.py`) if your tool needs auth

**Step 3** — Test it in Claude Code:

```
"Get recent news for TCS"
```

---

## Adding a new screening theme

Open `src/screener_mcp/tools/screening_tools.py` and add entries to both dicts:

```python
QUERY_TEMPLATES = {
    ...
    "my_theme": (
        "Return on capital employed > 20 AND "
        "Debt to equity < 0.5 AND "
        "Sales growth 5Years > 15"
    ),
}

THEME_DESCRIPTIONS = {
    ...
    "my_theme": "One-line description of what this screen finds",
}
```

That's it — the theme is immediately available via `screen_by_theme("my_theme")`.

---

## Screener.in query field names

Exact spelling matters. Common fields:

| Field | Notes |
|---|---|
| `Market Capitalization` | ₹ Crore |
| `Current Price` | |
| `Price to Earning` | PE ratio |
| `Price to book value` | |
| `Return on capital employed` | % |
| `Return on equity` | % |
| `Debt to equity` | |
| `Sales growth 5Years` / `3Years` / `last year` | % |
| `Profit growth 5Years` / `3Years` / `last year` | % |
| `Dividend yield` | % |
| `Pledged percentage` | % |

Operators: `>`, `<`, `=`, `AND`

---

## Submitting a PR

1. Fork the repo and create a branch: `git checkout -b feat/my-tool`
2. Make your changes
3. Test with Claude Code (`claude mcp add screener-dev ...`)
4. Open a PR with a short description of what the tool does and an example query

---

## Questions?

Open an issue at [github.com/LogeshR15/screener-mcp/issues](https://github.com/LogeshR15/screener-mcp/issues).
Gmail; logeshl2003@gmail.com
