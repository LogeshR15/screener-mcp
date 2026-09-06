"""
Parse a Screener.in company page.

URL format:
  Standalone:    https://www.screener.in/company/{SYMBOL}/
  Consolidated:  https://www.screener.in/company/{SYMBOL}/consolidated/

The HTML contains named <section> blocks. We extract:
  - Company name, description, about text
  - Top ratios (PE, PB, ROCE, ROE, dividend yield, market cap, etc.)
  - Profit & Loss table
  - Balance Sheet table
  - Cash Flow table
  - Quarterly results table
  - Key ratios history table
  - Shareholding pattern table
  - Peer comparison table
"""

import re
from typing import Any
from bs4 import BeautifulSoup, Tag


# ─── helpers ──────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_number(text: str) -> str:
    """Keep numeric strings as-is (Screener uses ₹ crore notation already)."""
    return _clean(text)


def _table_to_list(table: Tag) -> list[dict[str, str]]:
    """Convert an HTML table to a list of row dicts keyed by header."""
    if not table:
        return []
    headers = [_clean(th.get_text()) for th in table.select("thead th")]
    rows = []
    for tr in table.select("tbody tr"):
        cells = [_clean(td.get_text()) for td in tr.select("td")]
        if cells:
            row = {}
            for i, h in enumerate(headers):
                row[h] = cells[i] if i < len(cells) else ""
            rows.append(row)
    return rows


def _section_table(soup: BeautifulSoup, section_id: str) -> list[dict[str, str]]:
    section = soup.find(id=section_id)
    if not section:
        return []
    table = section.find("table")
    return _table_to_list(table)


def _yearly_table(soup: BeautifulSoup, section_id: str) -> dict[str, Any]:
    """
    Parse a financial table that has years as columns.
    Returns:
      {
        "years": ["Mar 2019", ...],
        "rows": [{"label": "Sales", "values": ["1000", "1200", ...]}, ...]
      }
    """
    section = soup.find(id=section_id)
    if not section:
        return {"years": [], "rows": []}

    table = section.find("table")
    if not table:
        return {"years": [], "rows": []}

    header_row = table.find("thead")
    years = []
    if header_row:
        years = [_clean(th.get_text()) for th in header_row.find_all("th")]
        years = years[1:]  # first col is the label col

    rows = []
    for tr in table.select("tbody tr"):
        cells = [_clean(td.get_text()) for td in tr.find_all("td")]
        if not cells:
            continue
        label = cells[0]
        values = cells[1:]
        rows.append({"label": label, "values": values})

    return {"years": years, "rows": rows}


# ─── top-level parsers ─────────────────────────────────────────────────────────

def parse_overview(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    # Company name
    name_tag = soup.find("h1", class_=lambda c: c and "h2" in c)
    if not name_tag:
        name_tag = soup.find("h1")
    name = _clean(name_tag.get_text()) if name_tag else "Unknown"

    # BSE / NSE codes — strip "BSE: " / "NSE: " prefixes from link text
    codes_tag = soup.find("div", class_="company-links")
    bse, nse = "", ""
    if codes_tag:
        for a in codes_tag.find_all("a"):
            href = a.get("href", "")
            text = _clean(a.get_text())
            if "bseindia" in href:
                bse = re.sub(r"(?i)^BSE\s*:\s*", "", text).strip()
            elif "nseindia" in href:
                nse = re.sub(r"(?i)^NSE\s*:\s*", "", text).strip()

    # About text
    about_tag = soup.find(id="about")
    about = ""
    if about_tag:
        p = about_tag.find("p")
        about = _clean(p.get_text()) if p else ""

    # Sector / industry from market breadcrumb links (Screener renders these in
    # the peer-comparison section as <a href="/market/…" title="Sector|Industry">)
    sectors = []
    for a in soup.select('a[href*="/market/"]'):
        if a.get("title", "") in {"Sector", "Industry"}:
            text = _clean(a.get_text())
            if text and text not in sectors:
                sectors.append(text)

    # Top ratios — special-case "High / Low" which has two separate number spans
    ratios: dict[str, str] = {}
    high_52, low_52 = "", ""
    top_ratios = soup.find(id="top-ratios")
    if top_ratios:
        for li in top_ratios.find_all("li"):
            name_span = li.find("span", class_="name")
            if not name_span:
                continue
            key = _clean(name_span.get_text())
            if key == "High / Low":
                numbers = li.find_all("span", class_="number")
                if len(numbers) >= 2:
                    high_52 = _clean(numbers[0].get_text())
                    low_52 = _clean(numbers[1].get_text())
                elif numbers:
                    high_52 = _clean(numbers[0].get_text())
            else:
                val_span = li.find("span", class_="number")
                if not val_span:
                    val_span = li.find("span", class_=lambda c: c and "value" in str(c).lower())
                ratios[key] = _clean(val_span.get_text()) if val_span else ""

    # Current price is rendered inside top-ratios, not a separate element
    price = ratios.pop("Current Price", "")

    return {
        "name": name,
        "bse_code": bse,
        "nse_code": nse,
        "about": about,
        "sectors": sectors,
        "current_price": price,
        "52_week_high": high_52,
        "52_week_low": low_52,
        "key_ratios": ratios,
    }


def parse_profit_loss(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    return _yearly_table(soup, "profit-loss")


def parse_balance_sheet(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    return _yearly_table(soup, "balance-sheet")


def parse_cash_flow(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    return _yearly_table(soup, "cash-flow")


def parse_ratios(html: str) -> dict[str, Any]:
    """Historical key ratios table (PE, ROCE, ROE, etc. year by year)."""
    soup = BeautifulSoup(html, "lxml")
    return _yearly_table(soup, "ratios")


def parse_quarterly_results(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    return _yearly_table(soup, "quarters")


def parse_shareholding(html: str) -> dict[str, Any]:
    """
    Returns shareholding by category across quarters.
    """
    soup = BeautifulSoup(html, "lxml")
    section = soup.find(id="shareholding")
    if not section:
        return {"quarters": [], "rows": []}

    table = section.find("table")
    if not table:
        return {"quarters": [], "rows": []}

    header = table.find("thead")
    quarters = []
    if header:
        ths = header.find_all("th")
        quarters = [_clean(th.get_text()) for th in ths[1:]]

    rows = []
    for tr in table.select("tbody tr"):
        cells = [_clean(td.get_text()) for td in tr.find_all("td")]
        if cells:
            rows.append({"category": cells[0], "values": cells[1:]})

    return {"quarters": quarters, "rows": rows}


def parse_warehouse_id(html: str) -> str | None:
    """
    Extract the `data-warehouse-id` from the company page's #company-info div.

    This id (distinct from the company's numeric id) is required to call
    Screener.in's AJAX peer-comparison endpoint:
      GET /api/company/{warehouse_id}/peers/
    """
    soup = BeautifulSoup(html, "lxml")
    info = soup.find(id="company-info")
    if not info:
        return None
    return info.get("data-warehouse-id")


def parse_peers_ajax(html: str) -> list[dict[str, str]]:
    """
    Parse the peer comparison table returned by Screener.in's AJAX endpoint
    `/api/company/{warehouse_id}/peers/` (an HTML fragment, not a full page).

    Unlike the tables on a full company page, this fragment has no <thead> —
    the header row is the first <tr> inside <tbody> and is made of <th> cells,
    so it needs its own parsing instead of the standard `_table_to_list`.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []

    all_rows = table.select("tbody tr") or table.select("tr")
    if not all_rows:
        return []

    headers = [_clean(th.get_text()) for th in all_rows[0].find_all("th")]
    if not headers:
        return []

    rows = []
    for tr in all_rows[1:]:
        cells = [_clean(td.get_text()) for td in tr.find_all("td")]
        if not cells:
            continue
        row = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(headers)}
        rows.append(row)
    return rows


def parse_peers(html: str) -> list[dict[str, str]]:
    """
    Parse the peer comparison table from a full company page.

    Screener.in loads the peer table via AJAX after page load, so the initial
    HTML contains only the sector breadcrumb context, not the actual rows.
    Prefer `parse_peers_ajax` (against the AJAX endpoint) for real peer data —
    this function only returns sector breadcrumb context as a fallback.
    """
    soup = BeautifulSoup(html, "lxml")
    section = soup.find(id="peers")
    if not section:
        return []

    table = section.find("table")
    if table:
        return _table_to_list(table)

    # Table not present (AJAX-loaded) — extract sector breadcrumb as context
    breadcrumb = []
    for a in section.select('a[href*="/market/"]'):
        title = a.get("title", "")
        text = _clean(a.get_text())
        if title and text:
            breadcrumb.append({"label": title, "value": text})

    if breadcrumb:
        return [{"_note": "Peer table loads via AJAX. Sector context below:"}] + [
            {"Sector Level": b["label"], "Name": b["value"]} for b in breadcrumb
        ]
    return []


def parse_full_page(html: str) -> dict[str, Any]:
    """Parse everything from a single page load."""
    return {
        "overview": parse_overview(html),
        "profit_loss": parse_profit_loss(html),
        "balance_sheet": parse_balance_sheet(html),
        "cash_flow": parse_cash_flow(html),
        "quarterly_results": parse_quarterly_results(html),
        "ratios_history": parse_ratios(html),
        "shareholding": parse_shareholding(html),
        "peers": parse_peers(html),
    }
