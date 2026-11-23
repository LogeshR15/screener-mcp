# CLAUDE.md

The project rules for all AI agents live in AGENTS.md and are imported below.
Follow them. This file only adds notes specific to Claude Code.

@AGENTS.md

---

## Claude Code notes

### Testing your change against a live server
Register the local checkout as a separate MCP server so it doesn't clash with
an installed `screener` server:

```bash
claude mcp add screener-dev -s local -- "$(pwd)/.venv/bin/python" "$(pwd)/run_server.py"
```

MCP servers load at session start. After changing tool code, restart Claude
Code (or run `/mcp` and reconnect) before testing. Otherwise you are testing
the old code.

Try the tool with a natural prompt (e.g. "Get the red flags for ASIANPAINT")
and check that the envelope's `status`, `warnings` and `missing_fields` are
correct, not just that data came back.

### Working style
- Read `server.py` and the relevant `tools/` module before editing. Most tools
  follow the same pattern, so copy the closest existing one.
- Run `python -m pytest -q` and the ruff command from AGENTS.md before you say
  a task is done. Report failures with their output.
- Live calls (canary, real tools) are slow and rate-limited. Run them once, at
  the end, not in a loop.
- Don't commit, push or open a PR unless the contributor asks you to.
- `.claude/` is gitignored. Keep personal settings in
  `.claude/settings.local.json` and don't commit them.

### Credentials
`SCREENER_USERNAME` / `SCREENER_PASSWORD` come from the contributor's shell
environment. Never ask for them in chat, echo them, write them to a file, or
pass them on a command line.
