"""Refresh the recorded Screener pages the contract tests run against.

    python scripts/refresh_fixtures.py

Saves trimmed copies of a few live pages into tests/fixtures/. Run it when the
live canary reports a layout change, review the diff (that diff *is* the change
Screener made), then update the parsers / theme filters until the tests pass.
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screener_mcp.client import get_client  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ROWS_KEPT = 6

# name -> path. Industry/index tables define which columns sector themes can filter on.
PAGES = {
    "industry_page.html": "/market/IN07/IN0702/IN070201/IN070201001/",
    "pledged_company_page.html": "/company/JPPOWER/",
}


def trim_table(html: str) -> str:
    """Keep the header row and the first few data rows of the data table."""
    start = html.find('<table class="data-table')
    end = html.find("</table>", start)
    if start < 0 or end < 0:
        return html
    table = html[start:end]
    rows = re.split(r"(?=<tr)", table)
    return html[:start] + "".join(rows[:ROWS_KEPT + 1]) + html[end:]


def keep_analysis(html: str) -> str:
    """Keep only the pros/cons box (#analysis) — all the pledge parser reads."""
    start = html.find('<section id="analysis"')
    end = html.find("</section>", start)
    if start < 0 or end < 0:
        return html
    return "<html><body>" + html[start:end + len("</section>")] + "</body></html>"


async def main() -> None:
    client = await get_client()
    for name, path in PAGES.items():
        html = await client.get_html(path)
        if name == "industry_page.html":
            html = trim_table(html)
        elif name == "pledged_company_page.html":
            html = keep_analysis(html)
        (FIXTURES / name).write_text(html)
        print(f"saved {name} ({len(html) // 1024} KB) from {path}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
