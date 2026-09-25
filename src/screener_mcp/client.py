"""
HTTP client for Screener.in.

Authentication flow:
  1. Login with SCREENER_USERNAME + SCREENER_PASSWORD env vars.
  2. Session cookie is retained for subsequent requests.
  3. CSRF token extracted from pages and sent with POST/state-changing calls.
  4. If no credentials provided, operates in public mode (limited data).
"""

import os
import asyncio
import logging
import random
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.screener.in"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.screener.in/",
}


# Screener.in answers bursts with 429 Too Many Requests. Cap concurrent
# requests and retry 429/503 with backoff so fan-out tools (technical screens,
# comparisons) degrade into "slower" instead of "half the rows failed".
_MAX_CONCURRENCY = int(os.getenv("SCREENER_MAX_CONCURRENCY", "3"))
# Minimum spacing between request starts (seconds). Screener throttles bursts
# hard — a cold 50-stock screen fired 4-wide got 429s and then stalled
# requests — so pace requests instead of racing into the limit.
_MIN_INTERVAL = float(os.getenv("SCREENER_MIN_INTERVAL", "0.25"))
_MAX_RETRIES = 5
_RETRYABLE_TRANSPORT = (
    httpx.ConnectError, httpx.ProxyError, httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException,
)


class ScreenerClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._csrf_token: Optional[str] = None
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._pace_lock = asyncio.Lock()
        self._next_slot = 0.0       # monotonic time the next request may start
        self._cooldown_until = 0.0  # set by any 429 — pauses *all* requests

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    async def _wait_turn(self):
        """Global pacing: honour any active 429 cooldown, then space request
        starts at least _MIN_INTERVAL apart across all concurrent callers."""
        async with self._pace_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            start = max(now, self._next_slot, self._cooldown_until)
            self._next_slot = start + _MIN_INTERVAL
        if start > now:
            await asyncio.sleep(start - now)

    def _cool_down(self, seconds: float):
        """A 429 means the whole client is over the limit, not just this request."""
        until = asyncio.get_running_loop().time() + seconds
        self._cooldown_until = max(self._cooldown_until, until)

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        """GET with global pacing, bounded concurrency, a shared 429 cooldown,
        and retry on 429/503 and transient transport errors (incl. timeouts)."""
        await self._ensure_client()
        for attempt in range(_MAX_RETRIES + 1):
            async with self._semaphore:
                await self._wait_turn()
                try:
                    resp = await self._client.get(url, **kwargs)
                except _RETRYABLE_TRANSPORT:
                    if attempt == _MAX_RETRIES:
                        raise
                    self._cool_down(2 ** attempt)
                    continue
            if resp.status_code not in (429, 503) or attempt == _MAX_RETRIES:
                return resp
            retry_after = resp.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1)
            self._cool_down(min(delay, 60) + random.uniform(0, 1))
        return resp

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                timeout=30.0,
            )

    async def _get_csrf_token(self) -> str:
        """Extract CSRF token from Screener's login page."""
        await self._ensure_client()
        resp = await self._client.get(f"{BASE_URL}/login/")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        token_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
        if token_input:
            return token_input.get("value", "")
        # Fallback: extract from cookie
        return self._client.cookies.get("csrftoken", "")

    async def login(self) -> bool:
        """Login with env var credentials. Returns True if successful."""
        username = os.getenv("SCREENER_USERNAME", "")
        password = os.getenv("SCREENER_PASSWORD", "")
        if not username or not password:
            logger.info("No Screener credentials set — running in public mode (limited data)")
            return False

        async with self._lock:
            if self._logged_in:
                return True
            try:
                csrf = await self._get_csrf_token()
                resp = await self._client.post(
                    f"{BASE_URL}/login/",
                    data={
                        "username": username,
                        "password": password,
                        "csrfmiddlewaretoken": csrf,
                        "next": "/",
                    },
                    headers={"Referer": f"{BASE_URL}/login/"},
                )
                # Successful login redirects to home; still on /login/ means failure
                if "/login/" not in str(resp.url):
                    self._logged_in = True
                    logger.info("Logged in to Screener.in successfully")
                    return True
                logger.warning("Screener login failed — check credentials")
                return False
            except Exception as exc:
                logger.error("Login error: %s", exc)
                return False

    async def get_html(self, path: str, params: Optional[dict] = None) -> str:
        """Fetch an HTML page from Screener.in."""
        url = urljoin(BASE_URL, path)
        resp = await self._get(url, params=params or {})
        resp.raise_for_status()
        # Detect silent redirect to login/register page
        final_url = str(resp.url)
        if "/login/" in final_url or "/register/" in final_url:
            raise PermissionError(
                "Screener.in requires login for this feature. "
                "Set SCREENER_USERNAME and SCREENER_PASSWORD environment variables."
            )
        return resp.text

    async def get_json(self, path: str, params: Optional[dict] = None) -> dict | list:
        """Fetch a JSON endpoint from Screener.in."""
        url = urljoin(BASE_URL, path)
        resp = await self._get(
            url,
            params=params or {},
            headers={**DEFAULT_HEADERS, "Accept": "application/json, text/javascript, */*; q=0.01",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


# Module-level singleton
_client: Optional[ScreenerClient] = None


async def get_client() -> ScreenerClient:
    global _client
    if _client is None:
        _client = ScreenerClient()
        await _client.login()
    return _client
