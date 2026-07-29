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
        url = item.get("url", "")
        # The API appends a "Search everywhere: <query>" row pointing at
        # /full-text-search/ with a null id. It is not a company - drop it,
        # otherwise it is listed as a result with a nonsense symbol.
        if not url.strip("/").startswith("company/"):
            continue
        results.append({
            "name": item.get("name", ""),
            "url": url,
            "screener_id": _extract_id(url),
        })
    return results


def _extract_id(url: str) -> str:
    """
    Extract the symbol from a Screener company URL.

    /company/TCS/consolidated/        -> TCS
    /company/531569/consolidated/     -> 531569  (BSE code, used as the symbol)
    /company/id/246883/consolidated/  -> 246883  (internal-id form, used for
                                                  companies with no ticker)
    """
    parts = [p for p in url.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "company":
        # /company/id/<n>/... - the symbol is the segment after "id",
        # not the literal string "id".
        if parts[1] == "id" and len(parts) >= 3:
            return parts[2]
        return parts[1]
    if parts:
        return parts[-1]
    return url


def parse_screen_results(html: str) -> dict:
    """
    Parse the stock screener results page.
    Returns company list with available columns.
    """
    soup = BeautifulSoup(html, "lxml")

    result_count_tag = (
        soup.find(attrs={"data-page-info": True})
        or soup.find(class_="count-text")
        or soup.find(id="count")
    )
    result_count = _clean(result_count_tag.get_text()) if result_count_tag else "unknown"

    table = soup.find("table", id="data-table")
    if not table:
        table = soup.find("table", class_=lambda c: c and "data" in str(c))

    if not table:
        return {"count": result_count, "companies": [], "columns": []}

    # Screener's screen-results table has no <thead> — the header row is the
    # first <tr> in <tbody>, using <th> cells instead of <td>.
    header_cells = table.select("thead th")
    if not header_cells:
        first_row = table.find("tr")
        header_cells = first_row.find_all("th") if first_row else []

    headers = [_clean(th.get_text()) for th in header_cells]

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
