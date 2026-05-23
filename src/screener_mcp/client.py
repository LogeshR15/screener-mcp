"""
HTTP client for Screener.in.

Authentication flow:
  1. Login with SCREENER_USERNAME + SCREENER_PASSWORD env vars.
  2. Session cookie is retained for subsequent requests.
  3. CSRF token extracted from pages and sent with POST/state-changing calls.
  4. If no credentials provided, operates in public mode (limited data).
"""

import os
import re
import asyncio
import logging
from typing import Optional
from urllib.parse import urljoin, urlencode

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


class ScreenerClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._csrf_token: Optional[str] = None
        self._logged_in = False
        self._lock = asyncio.Lock()

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
        await self._ensure_client()
        url = urljoin(BASE_URL, path)
        resp = await self._client.get(url, params=params or {})
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
        await self._ensure_client()
        url = urljoin(BASE_URL, path)
        resp = await self._client.get(
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
