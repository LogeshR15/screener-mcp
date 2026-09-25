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
    """Extract symbol from URL like /company/TCS/ or /company/TCS/consolidated/"""
    parts = [p for p in url.strip("/").split("/") if p]
    if "company" in parts:
        idx = parts.index("company")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if parts:
        return parts[-1]
    return url


def parse_screen_results(html: str) -> dict:
    """
    Parse the stock screener results page.
    Returns company list with available columns.
    """
    soup = BeautifulSoup(html, "lxml")

    # Count is in <div class="sub">850 results found: ...</div>
    result_count = "unknown"
    for div in soup.find_all("div", class_="sub"):
        text = _clean(div.get_text())
        if "result" in text.lower():
            result_count = text
            break

    table = soup.find("table", class_="data-table")
    if not table:
        table = soup.find("table", id="data-table")
    if not table:
        table = soup.find("table", class_=lambda c: c and "data" in str(c))

    total_results, total_pages = _parse_counts(result_count)

    if not table:
        return {"count": result_count, "companies": [], "columns": [],
                "total_results": total_results, "total_pages": total_pages}

    # Headers are in the first <tr> using <th> tags (no <thead>)
    all_rows = table.find_all("tr")
    if not all_rows:
        return {"count": result_count, "companies": [], "columns": [],
                "total_results": total_results, "total_pages": total_pages}

    header_row = all_rows[0]
    headers = []
    for th in header_row.find_all("th"):
        a_tag = th.find("a")
        label = th.get("data-tooltip") or (a_tag.get_text(strip=True) if a_tag else _clean(th.get_text()))
        headers.append(_clean(label))

    # Data rows have data-row-company-id attribute
    companies = []
    for tr in all_rows[1:]:
        if not tr.get("data-row-company-id"):
            continue
        cells = tr.find_all("td")
        if not cells:
            continue
        row = {"_company_id": tr.get("data-row-company-id")}
        for i, h in enumerate(headers):
            cell = cells[i] if i < len(cells) else None
            if cell:
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
        "total_results": total_results,
        "total_pages": total_pages,
    }


def _parse_counts(text: str) -> tuple[int | None, int | None]:
    """'850 results found: Showing page 1 of 34' → (850, 34)."""
    results = re.search(r"([\d,]+)\s+results?", text or "")
    pages = re.search(r"page\s+\d+\s+of\s+(\d+)", text or "", re.I)
    return (
        int(results.group(1).replace(",", "")) if results else None,
        int(pages.group(1)) if pages else None,
    )
