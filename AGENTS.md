# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, Copilot, Aider, etc.)
working on **screener-mcp**: an MCP server that gives LLM clients live Indian
equity data from Screener.in, NSE, Yahoo Finance and Google News.

Human contributors should read [CONTRIBUTING.md](CONTRIBUTING.md). This file is
the short, rules-first version for agents. Where the two disagree, the code and
tests win. Point out the mismatch in your PR.

---

## Setup and commands

Python 3.11+. Work inside a virtualenv.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"          # core + pytest
# pip install -e ".[ai]"         # only if touching RAG/document tools (~1–2 GB, pulls torch)
```

Run these before you call a change done (CI runs the same):

```bash
python -m pytest -q                                    # offline, no network, no credentials
pipx run ruff check --select F src scripts tests       # unused imports / undefined names
python -m build                                        # package must still build
```

Optional, needs network, hits the live site:

```bash
python -u scripts/canary.py      # live parser checks against Screener.in / NSE
python run_server.py             # start the server over stdio
```

- **CI installs core deps only, without `[ai]`.** That is deliberate. Code under
  `src/` must import and the tests must pass without `pdfplumber`, `chromadb` or
  `sentence-transformers`. Import those lazily inside functions, never at module
  top level.
- Tests must stay offline. Don't add tests that make network calls or need
  `SCREENER_USERNAME` / `SCREENER_PASSWORD`. Use `monkeypatch` and the HTML in
  `tests/fixtures/`.

---

## Map of the code

| Path | What lives there |
|---|---|
| `src/screener_mcp/server.py` | Every `@mcp.tool()`, prompt and resource. Thin wrappers only. **Start here.** |
| `src/screener_mcp/tools/*.py` | Tool implementations (the `_`-prefixed imports in `server.py`) |
| `src/screener_mcp/core/envelope.py` | `ToolResult`, `ToolError`, `to_envelope`: the response contract |
| `src/screener_mcp/core/company_page.py` | Symbol resolution, page fetch, consolidated → standalone fallback |
| `src/screener_mcp/core/quality.py` | Missing-field detection and ratio sanity bounds |
| `src/screener_mcp/core/nse_client.py` | NSE API client (raises `NSEError`) |
| `src/screener_mcp/core/rag.py`, `vector_store.py` | Local PDF → chunks → embeddings → ChromaDB pipeline |
| `src/screener_mcp/parsers/` | HTML/JSON parsing for Screener pages |
| `src/screener_mcp/client.py` | Screener.in HTTP client and login |
| `src/screener_mcp/ui/` | Bundled MCP App dashboard (vendored JS; don't hand-edit `ext-apps-app-with-deps.js`) |
| `tests/` | Offline tests: registry/docs consistency, envelope, parsers, protocol |
| `scripts/canary.py` | Live checks, run daily in CI; failures open an issue |
| `docs/` | GitHub Pages landing site |

---

## Hard rules

These are enforced by tests or have caused real bugs. Don't break them.

### The response envelope
- Every tool in `server.py` returns `await _safe(impl)(...)`, or
  `_safe(impl, ctx)` for slow tools so progress notifications are sent. Never
  return raw data or let an exception escape a tool.
- Implementations return a `ToolResult`, a `dict` or a markdown `str`. For
  expected failures (bad input, unknown symbol) raise
  `ToolError(message, error_type, **details)` and put anything the caller can
  act on, such as `candidates=[...]`, in `details`.
- **Never turn missing data into a plausible-looking value.** No `0`, `""` or
  `"N/A"` standing in for a value that couldn't be read. Use `None`, list the
  field in `missing_fields` and set a `reason`. Blank data that still came back
  as `"ok"` is the main bug class this project has fixed. Don't bring it back.

### Tool surface and docs stay in sync
If you **add, remove or rename a tool**, update all of these in the same change.
`tests/test_tools.py` fails otherwise:
1. `EXPECTED_TOOLS` in `tests/test_tools.py`
2. The module docstring at the top of `server.py`, including the "(N total)" count
3. The README tool table and its `## Tools — N total` heading

Also update by hand (no test checks these): the tool count in `docs/index.html`
and the matching CONTRIBUTING/README prose.

If you add a **screening theme** to `THEMES` in `tools/screening_tools.py`, add
it to the README theme list too (a test checks this).

### Tool definitions
- Every tool needs `annotations=`. Use `_read_only("Title")` for fetch-only
  tools. Tools that write local state (portfolio, notebook) take an explicit
  `ToolAnnotations(read_only_hint=False, destructive_hint=..., ...)`.
  `tests/test_protocol.py` checks this.
- Return type is `dict[str, Any]`, so the tool advertises an `outputSchema`.
- The docstring is what the model reads to decide when and how to call the
  tool. Say what it does, describe each parameter in plain words and include a
  concrete example call. Keep it accurate. A wrong docstring is a bug.
- Don't expose `ctx` as a tool parameter in the schema (a test checks this).
- One tool, one job. Don't add speculative optional parameters.

### Fetching and parsing
- Fetch company pages through `fetch_company_page` (in `core/company_page.py`),
  never with a raw `httpx` call, so symbol resolution and the standalone
  fallback apply everywhere.
- If you parse a **new part** of a Screener/NSE page, add a check to
  `scripts/canary.py` so a future layout change fails loudly.
- Add or extend offline tests against a trimmed fixture in `tests/fixtures/`.
  Don't commit large raw page dumps.
- If you change PDF extraction or chunking in `core/rag.py`, bump
  `INDEX_VERSION` so existing user caches are rebuilt.

### Code hygiene
- **Every text file read or write passes `encoding="utf-8"`**: `open()`,
  `read_text()`, `write_text()`. Binary modes (`"rb"`, `"wb"`) take no encoding.
  `tests/test_encoding.py` scans the AST and fails otherwise (Windows cp1252
  regression, issue #18).
- Tools that need a Screener login follow the `_LOGIN_REQUIRED_MSG` pattern in
  `screening_tools.py`. Pure data tools must work without credentials.
- The HTTP transport binds to `127.0.0.1` by default. Don't change that default.
- Match the existing style: async functions, type hints, small modules, short
  comments that explain *why*. No new formatter or linter config without asking.

### Dependencies
- Core deps (`mcp`, `httpx`, `beautifulsoup4`, `lxml`) are kept minimal on
  purpose. Justify any new core dependency in the PR. Heavy or ML deps belong
  in the `[ai]` extra.
- Keep `pyproject.toml`, `requirements*.txt` and `server.json` consistent when
  you touch dependencies or the version.

---

## Never do this

- Commit credentials, `.env` files, `start_server.sh`, or anything from
  `~/.screener-mcp/` (user portfolio, notes, caches).
- Hard-code or log `SCREENER_USERNAME` / `SCREENER_PASSWORD`, or put them in
  test fixtures.
- Bump the version, edit `CHANGELOG.md` release headings, create tags or
  releases, or touch `.github/workflows/publish.yml` unless the maintainer asks.
  Releases are cut by the maintainer.
- Disable, skip or loosen a test to get CI green. Fix the code, or explain in
  the PR why the test is wrong.
- Hammer Screener.in or NSE in loops while debugging. They rate-limit (HTTP 429)
  and NSE blocks cloud IPs. Use fixtures.
- Present tool output as investment advice in docstrings, prompts or the server
  `instructions`.

---

## Pull requests

- Branch name: `feat/…`, `fix/…`, `docs/…`.
- Keep PRs focused: one feature or fix per PR, with no drive-by reformatting.
- Under `## Unreleased` in `CHANGELOG.md`, add a short entry in the existing
  style (Added / Changed / Fixed). Create the heading if it isn't there.
- The PR description should include:
  - what changed and why, in a sentence or two
  - for a new or changed tool, an example prompt that triggers it
  - any new dependency and why it's needed
  - **that the PR was written with an AI agent**, and which one
  - what you ran (pytest, ruff, canary) and the result
- If you couldn't run something (no network for the canary, no `[ai]` install),
  say so. Don't claim it passed.
