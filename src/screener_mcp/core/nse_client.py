"""
NSE India API client for company documents, announcements, and corporate filings.

NSE requires a browser-like session (homepage hit first to get cookies).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com",
}


class NSEClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._session_init = False
        self._lock = asyncio.Lock()

    async def _ensure_session(self):
        """Hit NSE homepage to prime session cookies."""
        if self._session_init:
            return
        async with self._lock:
            if self._session_init:
                return
            if self._client is None:
                self._client = httpx.AsyncClient(
                    headers=NSE_HEADERS,
                    follow_redirects=True,
                    timeout=30.0,
                )
            try:
                await self._client.get(NSE_BASE)
                await asyncio.sleep(0.5)
                self._session_init = True
            except Exception as e:
                logger.warning(f"NSE session init failed: {e}")

    async def get_json(self, path: str, params: dict = None) -> dict | list:
        await self._ensure_session()
        url = f"{NSE_BASE}{path}"
        resp = await self._client.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()

    async def get_annual_reports(self, symbol: str) -> list[dict]:
        """Fetch annual report links for a company from NSE."""
        try:
            data = await self.get_json(
                "/api/annual-reports",
                params={"index": "equities", "symbol": symbol.upper()},
            )
            reports = data.get("data", []) if isinstance(data, dict) else (data or [])
            result = []
            for r in reports:
                url = r.get("fileName") or r.get("pdfLink") or ""
                if not url:
                    continue
                to_yr = r.get("toYr", "")
                year = to_yr or r.get("fromYr", "")
                result.append({
                    "year": year,
                    "from_date": r.get("fromYr", ""),
                    "to_date": to_yr,
                    "title": r.get("companyName", "Annual Report"),
                    "url": url,
                    "type": "annual_report",
                    "exchange": "NSE",
                })
            return result
        except Exception as e:
            logger.warning(f"NSE annual reports failed for {symbol}: {e}")
            return []

    async def get_announcements(self, symbol: str) -> list[dict]:
        """Fetch recent company announcements from NSE."""
        try:
            data = await self.get_json(
                "/api/corporate-announcements",
                params={"index": "equities", "symbol": symbol.upper()},
            )
            items = data.get("data", []) if isinstance(data, dict) else (data or [])
            return [
                {
                    "date": item.get("an_dt", ""),
                    "category": item.get("desc", ""),
                    "headline": item.get("attchmntText", ""),
                    "url": item.get("attchmntFile", ""),
                    "exchange": "NSE",
                }
                for item in items
            ]
        except Exception as e:
            logger.warning(f"NSE announcements failed for {symbol}: {e}")
            return []

    async def get_bulk_deals(self, from_date: str, to_date: str, symbol: str = None) -> list[dict]:
        """Fetch NSE bulk deals (trades > 0.5% of equity)."""
        try:
            params = {"from": from_date, "to": to_date}
            if symbol:
                params["symbol"] = symbol.upper()
            data = await self.get_json("/api/bulk-deals", params=params)
            return data.get("data", []) if isinstance(data, dict) else (data or [])
        except Exception as e:
            logger.warning(f"NSE bulk deals failed: {e}")
            return []

    async def close(self):
        if self._client:
            await self._client.aclose()


_nse_client: Optional[NSEClient] = None


async def get_nse_client() -> NSEClient:
    global _nse_client
    if _nse_client is None:
        _nse_client = NSEClient()
    return _nse_client
