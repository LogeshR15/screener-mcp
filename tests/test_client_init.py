"""Offline regression tests for client start-up and server defaults."""

import asyncio

from screener_mcp import client as client_module
from screener_mcp import server as server_module


async def test_concurrent_first_callers_never_see_a_logged_out_client(monkeypatch):
    """A second caller arriving while login() is in flight must wait for it,
    not receive the half-initialised client."""
    monkeypatch.setattr(client_module, "_client", None)
    monkeypatch.setattr(client_module, "_client_init_lock", asyncio.Lock(), raising=False)

    logins = 0

    async def slow_login(self):
        nonlocal logins
        logins += 1
        await asyncio.sleep(0.05)
        self._logged_in = True
        return True

    monkeypatch.setattr(client_module.ScreenerClient, "login", slow_login)

    async def caller():
        c = await client_module.get_client()
        return c, c.logged_in  # state at the moment this caller gets it back

    results = await asyncio.gather(*(caller() for _ in range(5)))

    assert all(logged_in for _, logged_in in results)
    assert len({id(c) for c, _ in results}) == 1
    assert logins == 1


def test_http_server_binds_to_loopback_by_default():
    # Docker opts in to 0.0.0.0 via MCP_HOST; the library default must not
    # expose an unauthenticated server (holding credentials) to the network.
    assert server_module.HTTP_HOST == "127.0.0.1"


async def test_failed_login_is_retried_but_throttled(monkeypatch):
    """A start-up login failure must not leave the server in public mode
    forever, and must not retry on every single call either."""
    monkeypatch.setenv("SCREENER_USERNAME", "u")
    monkeypatch.setenv("SCREENER_PASSWORD", "p")
    monkeypatch.setattr(client_module, "_client", None)
    monkeypatch.setattr(client_module, "_client_init_lock", asyncio.Lock(), raising=False)

    attempts = 0
    succeed = False

    async def fake_csrf(self):
        nonlocal attempts
        attempts += 1
        if not succeed:
            raise RuntimeError("network blip")
        return "tok"

    class _Resp:
        url = "https://www.screener.in/"

    async def fake_post(*a, **kw):
        return _Resp()

    monkeypatch.setattr(client_module.ScreenerClient, "_get_csrf_token", fake_csrf)

    c = await client_module.get_client()          # start-up login fails
    assert not c.logged_in and attempts == 1

    await client_module.get_client()              # inside the throttle window
    assert attempts == 1

    c._last_login_attempt -= client_module._LOGIN_RETRY_SECONDS + 1
    succeed = True
    await c._ensure_client()
    monkeypatch.setattr(c._client, "post", fake_post)
    await client_module.get_client()              # window elapsed → retried
    assert attempts == 2 and c.logged_in
