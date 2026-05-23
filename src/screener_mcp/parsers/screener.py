"""
Parse Screener.in stock screener / search results.

Endpoints:
  Search:   GET /api/company/search/?q=<query>
            Returns JSON: [{"id": ..., "name": "...", "url": "..."}, ...]

  Screen:   GET /api/screen/?query=<query>&order=&sort=
            Returns HTML table of matching companies with metrics.

  Explore:  GET /explore/  — curated pre-built screens
"""

import re
from bs4 import BeautifulSoup


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_search_results(json_data: list) -> list[dict]:
    """Parse company search API response."""
    results = []
    for item in json_data:
        results.append({
            "name": item.get("name", ""),
            "url": item.get("url", ""),
            "screener_id": _extract_id(item.get("url", "")),
        })
    return results


def _extract_id(url: str) -> str:
    """Extract symbol from URL like /company/TCS/"""
    parts = [p for p in url.strip("/").split("/") if p]
    if parts:
        return parts[-1]
    return url


def parse_screen_results(html: str) -> dict:
    """
    Parse the stock screener results page.
    Returns company list with available columns.
    """
    soup = BeautifulSoup(html, "lxml")

    result_count_tag = soup.find(class_="count-text") or soup.find(id="count")
    result_count = _clean(result_count_tag.get_text()) if result_count_tag else "unknown"

    table = soup.find("table", id="data-table")
    if not table:
        table = soup.find("table", class_=lambda c: c and "data" in str(c))

    if not table:
        return {"count": result_count, "companies": [], "columns": []}

    headers = [_clean(th.get_text()) for th in table.select("thead th")]

    companies = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row = {}
        for i, h in enumerate(headers):
            cell = cells[i] if i < len(cells) else None
            if cell:
                # Extract link for name column
                a_tag = cell.find("a")
                if a_tag:
                    row[h] = _clean(a_tag.get_text())
                    row["_url"] = a_tag.get("href", "")
                else:
                    row[h] = _clean(cell.get_text())
        companies.append(row)

    return {
        "count": result_count,
        "columns": headers,
        "companies": companies,
    }
