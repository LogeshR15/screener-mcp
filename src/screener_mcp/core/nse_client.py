"""
NSE India API client for company documents, announcements, and corporate filings.

NSE requires a browser-like session (homepage hit first to get cookies).
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _xbrl_tag(xml: str, tag: str) -> str:
    """Pull a single `<in-bse-co:Tag ...>value</in-bse-co:Tag>` value out of a
    PIT disclosure XBRL document without a full XML parser — these filings
    are small, flat, and namespace-only, so a regex is simpler and avoids
    pulling in an XML dependency for one field extraction."""
    m = re.search(rf"<in-bse-co:{tag}[^>]*>([^<]*)</in-bse-co:{tag}>", xml)
    return m.group(1).strip() if m else ""

class NSEError(Exception):
    """NSE's API failed or blocked the request (as opposed to returning no rows).

    NSE frequently rate-limits or 403s server IPs. Callers must surface this —
    swallowing it into an empty list made "NSE blocked us" indistinguishable
    from "the company has no announcements".
    """


NSE_BASE = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # No "br" (Brotli) — httpx can only decode it if the optional `brotli`/
    # `brotlicffi` package is installed, and NSE serves Brotli whenever it's
    # offered. Without that package, httpx silently returns undecoded bytes,
    # every call to get_json() fails to parse as JSON, and callers here catch
    # the exception and report an empty result — so requesting Brotli when
    # nothing can decode it turns real data into "no announcements found".
    "Accept-Encoding": "gzip, deflate",
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
        try:
            resp = await self._client.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            hint = " (NSE often blocks server/cloud IPs or rate-limits bursts)" if code in (401, 403, 429) else ""
            raise NSEError(f"NSE API returned HTTP {code} for {path}{hint}") from e
        except httpx.HTTPError as e:
            raise NSEError(f"Could not reach NSE API ({type(e).__name__}) for {path}") from e
        except ValueError as e:
            raise NSEError(f"NSE API returned a non-JSON response for {path} — likely a block/captcha page") from e

    async def get_annual_reports(self, symbol: str) -> list[dict]:
        """Fetch annual report links for a company from NSE. Raises NSEError on failure."""
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

    async def get_announcements(self, symbol: str) -> list[dict]:
        """Fetch recent company announcements from NSE. Raises NSEError on failure."""
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

    async def _get_bulk_deals_for_day(self, date_str: str, symbol: str = None) -> list[dict]:
        params = {"optionType": "bulk_deals", "from": date_str, "to": date_str}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await self.get_json("/api/historicalOR/bulk-block-short-deals", params=params)
        items = data.get("data", []) if isinstance(data, dict) else (data or [])
        return [
            {
                "date": item.get("BD_DT_DATE", ""),
                "symbol": item.get("BD_SYMBOL", ""),
                "scripName": item.get("BD_SCRIP_NAME", ""),
                "clientName": item.get("BD_CLIENT_NAME", ""),
                "buySell": item.get("BD_BUY_SELL", ""),
                "quantityTraded": item.get("BD_QTY_TRD", ""),
                "tradePrice": item.get("BD_TP_WATP", ""),
            }
            for item in items
        ]

    # NSE's `/api/historicalOR/bulk-block-short-deals` endpoint (the successor
    # to the old, now-404 `/api/bulk-deals`) ignores `to` when it differs from
    # `from` and only ever returns the single trading day at `from` — there is
    # no way to fetch a real multi-day range in one call. To cover a window we
    # fire one request per calendar day (NSE just returns [] for non-trading
    # days) and merge the results. Capped so a `days=365` request can't turn
    # into 365 serial round-trips to a third-party site.
    MAX_BULK_DEAL_DAYS = 90

    async def get_bulk_deals(
        self, from_date: str, to_date: str, symbol: str = None
    ) -> tuple[list[dict], list[str], int]:
        """Fetch NSE bulk deals (trades > 0.5% of equity) for a date range.

        Returns (rows, failed_dates, days_queried). Only the most recent
        `MAX_BULK_DEAL_DAYS` calendar days of the requested range are queried
        (see note above). Raises NSEError if every day failed, so a total
        outage never reads as "no bulk deals".
        """
        end = datetime.strptime(to_date, "%d-%m-%Y")
        start = datetime.strptime(from_date, "%d-%m-%Y")

        span_days = max((end - start).days + 1, 1)
        query_days = min(span_days, self.MAX_BULK_DEAL_DAYS)
        dates = [(end - timedelta(days=i)).strftime("%d-%m-%Y") for i in range(query_days)]

        sem = asyncio.Semaphore(8)

        async def _fetch(d: str) -> list[dict]:
            async with sem:
                return await self._get_bulk_deals_for_day(d, symbol)

        results = await asyncio.gather(*[_fetch(d) for d in dates], return_exceptions=True)
        failed = [d for d, r in zip(dates, results) if isinstance(r, Exception)]
        if failed and len(failed) == len(dates):
            raise NSEError(f"All {len(dates)} daily NSE bulk-deal requests failed: {results[0]}")
        rows = [row for r in results if not isinstance(r, Exception) for row in r]
        return rows, failed, len(dates)

    # How many of the most recent filings to open and parse into full trade
    # detail. The list endpoint is one cheap call; each disclosure's XBRL is
    # a separate download, so this bounds a single tool call to a handful of
    # extra round-trips regardless of a company's filing history.
    MAX_INSIDER_FILINGS = 15

    async def get_insider_trading(self, symbol: str) -> list[dict]:
        """Fetch SEBI PIT Regulation 7(2) insider-trading disclosures for a
        company — promoter/KMP/designated-person trades, reported at any
        size (unlike bulk deals, which only capture trades > 0.5% of equity).

        NSE's list endpoint only returns filing metadata (who filed, when,
        and a link to the underlying XBRL); the actual trade details (buyer/
        seller name, buy/sell, quantity, value, mode of acquisition) live in
        each filing's XBRL document, so this fetches and parses those too.
        """
        data = await self.get_json(
            "/api/corporates-pit-gg",
            params={"index": "equities", "symbol": symbol.upper()},
        )
        filings = data.get("data", []) if isinstance(data, dict) else (data or [])

        filings = filings[: self.MAX_INSIDER_FILINGS]
        sem = asyncio.Semaphore(8)

        async def _fetch_detail(filing: dict) -> dict:
            url = filing.get("xmlFileName", "")
            if not url:
                return {**filing, "_detail_error": True}
            async with sem:
                try:
                    resp = await self._client.get(url, timeout=30.0)
                    resp.raise_for_status()
                    xml = resp.text
                except Exception as e:
                    logger.warning(f"NSE insider trading XBRL fetch failed for {url}: {e}")
                    return {**filing, "_detail_error": True}
            return {
                **filing,
                "personName": _xbrl_tag(xml, "NameOfThePerson"),
                "personCategory": _xbrl_tag(xml, "CategoryOfPerson"),
                "transactionType": _xbrl_tag(xml, "SecuritiesAcquiredOrDisposedTransactionType"),
                "securitiesTraded": _xbrl_tag(xml, "SecuritiesAcquiredOrDisposedNumberOfSecurity"),
                "tradeValue": _xbrl_tag(xml, "SecuritiesAcquiredOrDisposedValueOfSecurity"),
                "modeOfAcquisition": _xbrl_tag(xml, "ModeOfAcquisitionOrDisposal"),
                "holdingPrePct": _xbrl_tag(xml, "SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding"),
                "holdingPostPct": _xbrl_tag(xml, "SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding"),
                "transactionFromDate": _xbrl_tag(xml, "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate"),
                "transactionToDate": _xbrl_tag(xml, "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate"),
            }

        await self._ensure_session()
        return await asyncio.gather(*[_fetch_detail(f) for f in filings])

    async def get_insider_trades(self, symbol: str, days: int = 365) -> list[dict]:
        """Structured PIT trades from `/api/corporates-pit` for the last `days`.

        This endpoint carries the trade fields inline (no XBRL download) and
        covers disclosures the newer `corporates-pit-gg` feed doesn't list —
        but without a date range it returns a stale default page, so the range
        is always sent.
        """
        end = datetime.now()
        start = end - timedelta(days=days)
        data = await self.get_json(
            "/api/corporates-pit",
            params={"index": "equities", "symbol": symbol.upper(),
                    "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")},
        )
        return data.get("data", []) if isinstance(data, dict) else (data or [])

    async def close(self):
        if self._client:
            await self._client.aclose()


_nse_client: Optional[NSEClient] = None


async def get_nse_client() -> NSEClient:
    global _nse_client
    if _nse_client is None:
        _nse_client = NSEClient()
    return _nse_client
