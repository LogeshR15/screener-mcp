"""
Yahoo Finance quoteSummary client — used for analyst consensus targets.

quoteSummary needs a session cookie plus a "crumb" token: hit fc.yahoo.com to
get the cookie, then /v1/test/getcrumb for the crumb, and send both. The crumb
is cached for the process lifetime and refreshed once on a 401.

NSE listings are `<SYMBOL>.NS`, BSE listings `<CODE>.BO`.
"""

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# A bare "Mozilla/5.0" gets a crumb; a full desktop-Chrome UA string is answered
# with 429 on /getcrumb (observed 2026-09).
_UA = {"User-Agent": "Mozilla/5.0"}
_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")


class YahooError(Exception):
    """Yahoo Finance couldn't be reached or refused the request."""


class YahooClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._crumb: Optional[str] = None
        self._lock = asyncio.Lock()

    async def _ensure(self, refresh: bool = False):
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(headers=_UA, follow_redirects=True, timeout=15.0)
            if self._crumb and not refresh:
                return
            try:
                await self._client.get("https://fc.yahoo.com")  # sets the session cookie (404 is normal)
                resp = None
                for host in _HOSTS:
                    resp = await self._client.get(f"https://{host}/v1/test/getcrumb")
                    if resp.status_code == 200:
                        break
            except httpx.HTTPError as e:
                raise YahooError(f"Couldn't start a Yahoo Finance session ({type(e).__name__})") from e
            if resp is None or resp.status_code != 200:
                code = resp.status_code if resp is not None else "no response"
                raise YahooError(f"Yahoo Finance refused a session (HTTP {code})")
            crumb = resp.text.strip()
            if not crumb or "<" in crumb:
                raise YahooError("Yahoo Finance didn't issue a session crumb")
            self._crumb = crumb

    async def quote_summary(self, ticker: str, modules: list[str]) -> Optional[dict]:
        """quoteSummary result for ``ticker``, or None if Yahoo has no such ticker."""
        await self._ensure()
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        for attempt in range(2):
            try:
                resp = await self._client.get(url, params={"modules": ",".join(modules), "crumb": self._crumb})
            except httpx.HTTPError as e:
                raise YahooError(f"Yahoo Finance unreachable ({type(e).__name__})") from e
            if resp.status_code == 401 and attempt == 0:
                await self._ensure(refresh=True)
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise YahooError(f"Yahoo Finance returned HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError as e:
                raise YahooError("Yahoo Finance returned a non-JSON response") from e
            result = (body.get("quoteSummary") or {}).get("result")
            return result[0] if result else None
        raise YahooError("Yahoo Finance rejected the session crumb")


_client: Optional[YahooClient] = None


def get_yahoo_client() -> YahooClient:
    global _client
    if _client is None:
        _client = YahooClient()
    return _client


def raw(block: dict, key: str):
    """Yahoo wraps numbers as {"raw": 12.3, "fmt": "12.30"}."""
    v = (block or {}).get(key)
    return v.get("raw") if isinstance(v, dict) else v
