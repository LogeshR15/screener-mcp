"""
End-to-end protocol tests: a real MCP client talking to the server in-process.

They check what a client sees on the wire — tool schemas, annotations,
isError on failures, progress notifications, prompts — rather than calling
the tool functions directly. Offline: the tool implementations are stubbed.
"""

import asyncio

from mcp import Client

from screener_mcp import server
from screener_mcp.core.envelope import ToolError


async def test_server_identity():
    async with Client(server.mcp) as client:
        info = client.server_info
        assert info.name == "screener-mcp"
        assert info.title == "Screener Stock Research"
        assert info.website_url
        assert info.icons


async def test_context_param_is_hidden_from_tool_schemas():
    async with Client(server.mcp) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 30
        for t in tools:
            assert "ctx" not in t.input_schema.get("properties", {}), t.name
            assert t.annotations is not None and t.annotations.title, t.name
            assert t.output_schema and t.output_schema["type"] == "object", t.name


async def test_annotations_mark_write_tools():
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["search_company"].annotations.read_only_hint is True
        assert tools["remove_portfolio_stock"].annotations.read_only_hint is False
        assert tools["remove_portfolio_stock"].annotations.destructive_hint is True
        assert tools["add_portfolio_stock"].annotations.destructive_hint is False


async def test_error_envelope_sets_is_error(monkeypatch):
    async with Client(server.mcp) as client:
        async def boom(query):
            raise ToolError("No company matches 'zzz'", "not_found")

        monkeypatch.setattr(server, "_search_company", boom)
        res = await client.call_tool("search_company", {"query": "zzz"})
        assert res.is_error is True
        assert res.structured_content["status"] == "error"
        assert res.structured_content["error"]["type"] == "not_found"


async def test_success_is_not_an_error(monkeypatch):
    async with Client(server.mcp) as client:
        async def found(query):
            return {"matches": [{"symbol": "ITC"}]}

        monkeypatch.setattr(server, "_search_company", found)
        res = await client.call_tool("search_company", {"query": "itc"})
        assert res.is_error is False
        assert res.structured_content["status"] == "ok"


async def test_slow_tools_send_progress(monkeypatch):
    async with Client(server.mcp) as client:
        monkeypatch.setattr(server, "_PROGRESS_INTERVAL", 0.05)

        async def slow_outlook(symbol, days, include_earnings_call):
            await asyncio.sleep(0.3)
            return {"symbol": symbol}

        monkeypatch.setattr(server, "_get_forward_outlook", slow_outlook)
        updates = []

        async def on_progress(progress, total, message):
            updates.append((progress, message))

        res = await client.call_tool("get_forward_outlook", {"symbol": "ITC"}, progress_callback=on_progress)
        assert res.is_error is False
        assert len(updates) >= 2
        assert [p for p, _ in updates] == sorted(p for p, _ in updates)
        assert "still working" in updates[0][1]


async def test_prompts():
    async with Client(server.mcp) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert {"deep_dive", "value_screen"} <= names
        got = await client.get_prompt("deep_dive", {"symbol": "ITC"})
        assert "get_full_analysis('ITC')" in got.messages[0].content.text


def test_server_json_matches_package_version():
    """server.json (MCP Registry listing) must be bumped with pyproject.toml."""
    import json
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    listing = json.loads((root / "server.json").read_text(encoding="utf-8"))
    assert listing["version"] == version
    assert all(p["version"] == version for p in listing["packages"])
    assert f"mcp-name: {listing['name']}" in (root / "README.md").read_text(encoding="utf-8")
