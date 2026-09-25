"""
Document analysis tools — annual reports and earnings call transcripts.

Fetches document links from Screener.in, downloads PDFs, and answers
questions via semantic search (RAG: chromadb + sentence-transformers).
"""

import logging
import re

from bs4 import BeautifulSoup

from ..core.company_page import fetch_company_page
from ..core.envelope import ToolError, ToolResult
from ..core.nse_client import NSEError, get_nse_client
from ..core.rag import process_document, query_document, query_documents
from ..core.vector_store import get_vector_store

logger = logging.getLogger(__name__)


_ANNUAL_REPORT_RE = re.compile(r"annual\s*report", re.I)
_ANNUAL_REPORT_EXCLUDE_RE = re.compile(
    r"general\s*meeting|\bagm\b|newspaper|notice|brsr|business\s*responsibility",
    re.I,
)


def _looks_like_annual_report(href: str, text: str) -> bool:
    combined = f"{text} {href}"
    if _ANNUAL_REPORT_EXCLUDE_RE.search(combined):
        return False
    return bool(_ANNUAL_REPORT_RE.search(combined))


def _parse_annual_reports(html: str) -> list[dict]:
    """Extract annual report PDF links from the Screener.in company page."""
    soup = BeautifulSoup(html, "lxml")
    reports = []

    for section_id in ["annual-reports", "documents", "filings"]:
        section = soup.find(id=section_id)
        if not section:
            continue
        for a in section.find_all("a", href=True):
            href = a["href"]
            text = re.sub(r"\s+", " ", a.get_text()).strip()
            if not _looks_like_annual_report(href, text):
                continue
            year = re.search(r"20\d{2}", text + " " + href)
            url = href if href.startswith("http") else f"https://www.screener.in{href}"
            reports.append({
                "year": year.group() if year else "Unknown",
                "title": text or "Annual Report",
                "url": url,
                "type": "annual_report",
                "source": "screener",
            })
        break

    if not reports:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = re.sub(r"\s+", " ", a.get_text()).strip()
            if not _looks_like_annual_report(href, text):
                continue
            year = re.search(r"20\d{2}", text + " " + href)
            url = href if href.startswith("http") else f"https://www.screener.in{href}"
            reports.append({
                "year": year.group() if year else "Unknown",
                "title": text or "Annual Report",
                "url": url,
                "type": "annual_report",
                "source": "screener",
            })

    return reports


def _parse_earnings_calls(html: str) -> list[dict]:
    """Extract earnings call transcript links from the Screener.in company page.

    Screener nests these under `#documents .concalls`, not a dedicated
    "concalls"/"transcripts" element id — each entry is an `<li>` whose first
    text node is a "Mon YYYY" period label (not a proper "Q_FY__" quarter),
    followed by chip links ("Transcript", "AI Summary", "PPT", "REC").
    """
    soup = BeautifulSoup(html, "lxml")
    transcripts = []

    documents_section = soup.find(id="documents")
    concalls_box = documents_section.find(class_="concalls") if documents_section else None
    if not concalls_box:
        return transcripts

    for li in concalls_box.select("ul.list-links li"):
        link = next(
            (a for a in li.find_all("a", href=True) if "transcript" in a.get_text(strip=True).lower()),
            None,
        )
        if not link:
            continue
        href = link["href"]
        # The period label is the <li>'s leading text, before the chip links.
        period = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).split(" Transcript")[0].strip()
        quarter = re.search(r"Q[1-4]\s*FY?\s*\d{2,4}", period, re.I)
        url = href if href.startswith("http") else f"https://www.screener.in{href}"
        transcripts.append({
            "quarter": quarter.group().upper().replace(" ", "") if quarter else period or "Unknown",
            "title": f"Earnings Call Transcript — {period}" if period else "Earnings Call Transcript",
            "url": url,
            "type": "earnings_call",
            "source": "screener",
        })

    return transcripts


def _freshness_warnings(label: str, status: dict) -> list[str]:
    f = status.get("freshness") or {}
    if f.get("source_changed"):
        return [f"{label}: the source PDF appears to have changed since it was indexed "
                f"(indexed {f.get('last_indexed_at')}). Pass force_reindex=True to rebuild."]
    if status.get("status") == "cached" and not f.get("last_indexed_at"):
        return [f"{label}: cached index predates freshness tracking — age unknown. "
                "Pass force_reindex=True to rebuild and start tracking."]
    return []


def _source_info(status: dict) -> str:
    f = status.get("freshness") or {}
    if status["status"] == "cached":
        when = f.get("last_indexed_at") or "unknown time"
        return f"cached index ({status['chunks']} chunks, indexed {when})"
    return f"freshly indexed ({status['chunks']} chunks across {status.get('pages', '?')} pages)"


async def get_document_list(symbol: str) -> ToolResult:
    """List all available annual reports and earnings call transcripts for a company."""
    nse = await get_nse_client()

    page = await fetch_company_page(symbol, "consolidated")
    symbol, html = page.symbol, page.html
    screener_reports = _parse_annual_reports(html)
    screener_calls = _parse_earnings_calls(html)

    nse_reports = []
    nse_warning = []
    if not screener_reports:
        try:
            nse_reports = await nse.get_annual_reports(symbol)
        except NSEError as e:
            nse_warning = [f"NSE annual-report lookup failed ({e}) — list may be incomplete."]

    all_reports = screener_reports or nse_reports
    all_calls = screener_calls

    lines = [f"# Documents Available — {symbol.upper()}", ""]

    if all_reports:
        lines.append(f"## Annual Reports ({len(all_reports)} found)")
        for r in sorted(all_reports, key=lambda x: x.get("year", ""), reverse=True):
            lines.append(f"  [{r['year']}] {r['title']}")
            lines.append(f"          URL: {r['url']}")
    else:
        lines.append("## Annual Reports")
        lines.append("  None found via Screener.in or NSE API.")
        lines.append("  Tip: Check the company's investor relations page directly.")

    lines.append("")

    if all_calls:
        lines.append(f"## Earnings Call Transcripts ({len(all_calls)} found)")
        for c in all_calls:
            lines.append(f"  [{c['quarter']}] {c['title']}")
            lines.append(f"          URL: {c['url']}")
    else:
        lines.append("## Earnings Call Transcripts")
        lines.append("  None found on Screener.in.")

    lines.append("")
    lines.append("Use `analyze_annual_report(symbol, year, question)` or")
    lines.append("`analyze_earnings_call(symbol, quarter, question)` to ask questions about these documents.")
    lines.append("You can also pass `pdf_url` directly if you have the link.")

    return ToolResult(
        data={"report": "\n".join(lines)},
        warnings=page.warnings + nse_warning,
        partial=bool(nse_warning),
        reason=nse_warning[0] if nse_warning else None,
        meta=page.meta,
    )


async def analyze_annual_report(
    symbol: str,
    year: int,
    question: str,
    pdf_url: str = None,
    force_reindex: bool = False,
) -> ToolResult:
    """
    Ask any question about a company's annual report using semantic search over the PDF.

    If pdf_url is not given, fetches it automatically via Screener.in / NSE.
    """
    if not question.strip():
        question = "Summarize the key business highlights, financial performance, risks, and management commentary from this annual report."

    warnings: list[str] = []
    symbol = symbol.upper().strip()
    if not pdf_url:
        page = await fetch_company_page(symbol, "consolidated")
        symbol, html = page.symbol, page.html
        warnings += page.warnings
        reports = _parse_annual_reports(html)

        if not reports:
            nse = await get_nse_client()
            try:
                reports = await nse.get_annual_reports(symbol)
            except NSEError as e:
                warnings.append(f"NSE annual-report lookup failed ({e}).")

        matched = [r for r in reports if str(year) in str(r.get("year", ""))]
        if not matched:
            available = sorted({r.get("year") for r in reports}, reverse=True)
            raise ToolError(
                (
                    f"**Annual report for {symbol.upper()} ({year}) not found.**\n\n"
                    f"Available years: {', '.join(str(y) for y in available) or 'None found'}\n\n"
                    f"Use `get_document_list('{symbol}')` to see what's available, or pass `pdf_url` directly."
                ),
                "not_found",
            )
        pdf_url = matched[0]["url"]

    collection_name = f"{symbol}_{year}_annual"

    status = await process_document(
        pdf_url,
        collection_name,
        force=force_reindex,
        extra_metadata={"symbol": symbol, "doc_type": "annual_report", "label": str(year)},
    )
    if status["status"] == "error":
        raise ToolError(
            (
                f"**Failed to process annual report PDF.**\n\n"
                f"Error: {status.get('error')}\n"
                f"URL: {pdf_url}\n\n"
                f"Common causes:\n"
                f"  - PDF is scanned/image-only (no machine-readable text)\n"
                f"  - URL requires authentication\n"
                f"  - pdfplumber or sentence-transformers not installed\n\n"
                f"Run: `pip install pdfplumber sentence-transformers chromadb`"
            ),
            "document_error",
        )

    chunks = await query_document(collection_name, question, top_k=5)
    if not chunks:
        return f"**No relevant content found** for: '{question}'\n\nThe document was indexed but no matching sections were found. Try rephrasing your question."

    context = "\n\n---\n\n".join(
        f"[Excerpt {i} — Pages {c['metadata'].get('pages', '?')}]\n{c['text']}"
        for i, c in enumerate(chunks, 1)
    )
    source_info = _source_info(status)
    warnings += _freshness_warnings(f"Annual Report {year}", status)

    report = f"""# Annual Report Analysis — {symbol} ({year})

**Question:** {question}
**Document:** Annual Report {year} | {source_info}

## Relevant Excerpts

{context}

---
**Analyst task:** Using the excerpts above, answer: "{question}"

Structure your response as:
1. **Direct Answer** — what the document says
2. **Supporting Evidence** — specific data points from the excerpts
3. **Page References** — cite page numbers where relevant
4. **Caveats** — anything incomplete or that warrants a closer look at the full report
"""
    return ToolResult(
        data={"report": report, "document": {"url": pdf_url, "freshness": status.get("freshness")}},
        warnings=warnings,
    )


_FY_QUARTER_RE = re.compile(r"^Q([1-4])FY(\d{2,4})$")
# Indian fiscal year FY25 = Apr 2024-Mar 2025. Concalls follow quarter-end by
# ~2-6 weeks: Q1 (Apr-Jun) -> Jul, Q2 (Jul-Sep) -> Oct, both in the FY's
# *start* calendar year; Q3 (Oct-Dec) -> Jan, Q4 (Jan-Mar) -> Apr/May, both
# in the FY's *end* calendar year. Screener labels transcripts with a plain
# "Mon YYYY" (e.g. "Jul 2024"), not a fiscal-quarter string — this bridges
# a "Q1FY25"-style request to that label.
_QUARTER_END_MONTHS = {
    "1": ("JUL", "start"), "2": ("OCT", "start"),
    "3": ("JAN", "end"), "4": ("APR", "end"),
}


def _quarter_to_month_labels(quarter_clean: str) -> set[str]:
    """Map "Q1FY25" style input to the "MONYYYY" labels Screener actually uses."""
    m = _FY_QUARTER_RE.match(quarter_clean)
    if not m:
        return set()
    q, fy = m.group(1), m.group(2)
    fy_num = int(fy) if len(fy) == 2 else int(fy) % 100
    fy_start_year, fy_end_year = 2000 + fy_num - 1, 2000 + fy_num
    month, which_year = _QUARTER_END_MONTHS[q]
    year = fy_end_year if which_year == "end" else fy_start_year
    labels = {f"{month}{year}"}
    if q == "4":
        labels.add(f"MAY{year}")
    return labels


async def analyze_earnings_call(
    symbol: str,
    quarter: str,
    question: str,
    pdf_url: str = None,
    force_reindex: bool = False,
) -> ToolResult:
    """
    Ask any question about an earnings call transcript.

    quarter: e.g., "Q1FY25", "Q2FY26", "Q3FY25"
    """
    if not question.strip():
        question = "What did management say about revenue, margins, business outlook, and key risks?"

    quarter_clean = quarter.upper().replace(" ", "")
    warnings: list[str] = []
    symbol = symbol.upper().strip()

    if not pdf_url:
        page = await fetch_company_page(symbol, "consolidated")
        symbol, html = page.symbol, page.html
        warnings += page.warnings
        calls = _parse_earnings_calls(html)

        candidates = {quarter_clean} | _quarter_to_month_labels(quarter_clean)
        matched = [
            c for c in calls
            if any(cand in c.get("quarter", "").upper().replace(" ", "") for cand in candidates)
        ]
        if not matched and calls:
            available = [c.get("quarter") for c in calls]
            raise ToolError(
                (
                    f"**Earnings call for {symbol.upper()} ({quarter}) not found.**\n\n"
                    f"Available quarters: {', '.join(available)}\n\n"
                    f"Use `get_document_list('{symbol}')` to see all transcripts, or pass `pdf_url` directly."
                ),
                "not_found",
            )
        if not matched:
            raise ToolError(
                (
                    f"**No earnings call transcripts found for {symbol.upper()} on Screener.in.**\n\n"
                    f"You can pass the PDF URL directly via `pdf_url` parameter."
                ),
                "not_found",
            )
        pdf_url = matched[0]["url"]

    collection_name = f"{symbol}_{quarter_clean}_transcript"

    status = await process_document(
        pdf_url,
        collection_name,
        force=force_reindex,
        extra_metadata={"symbol": symbol, "doc_type": "earnings_call", "label": quarter_clean},
    )
    if status["status"] == "error":
        raise ToolError(
            (
                f"**Failed to process earnings call transcript.**\n\n"
                f"Error: {status.get('error')}\n"
                f"URL: {pdf_url}\n\n"
                f"Run: `pip install pdfplumber sentence-transformers chromadb`"
            ),
            "document_error",
        )

    chunks = await query_document(collection_name, question, top_k=5)
    if not chunks:
        return f"**No relevant content found** for: '{question}'"

    context = "\n\n---\n\n".join(
        f"[Excerpt {i} — Pages {c['metadata'].get('pages', '?')}]\n{c['text']}"
        for i, c in enumerate(chunks, 1)
    )
    source_info = _source_info(status)
    warnings += _freshness_warnings(f"Earnings call {quarter_clean}", status)

    report = f"""# Earnings Call Analysis — {symbol} ({quarter})

**Question:** {question}
**Document:** Earnings Call Transcript {quarter} | {source_info}

## Relevant Excerpts from Transcript

{context}

---
**Analyst task:** Based on the transcript excerpts above, answer: "{question}"

Focus on:
- What management explicitly said (direct quotes where possible)
- Specific guidance numbers (revenue, margins, volumes, capex)
- Management tone — confident, cautious, defensive?
- Any surprises vs. expectations
- Forward-looking statements and their credibility
"""
    return ToolResult(
        data={"report": report, "document": {"url": pdf_url, "freshness": status.get("freshness")}},
        warnings=warnings,
    )


async def ask_company_research(
    symbol: str,
    question: str,
    max_annual_reports: int = 3,
    max_earnings_calls: int = 4,
    include_annual_reports: bool = True,
    include_earnings_calls: bool = True,
) -> str:
    """
    Ask a question that's answered by searching across ALL of a company's cached
    documents at once (multiple annual reports + earnings call transcripts),
    instead of one document at a time like analyze_annual_report/analyze_earnings_call.

    Good for cross-year/cross-quarter questions, e.g. "how has capex strategy
    evolved over the last 3 years?" — something a single-document query can't answer.

    Indexes (or reuses already-cached indexes for) the most recent
    `max_annual_reports` annual reports and `max_earnings_calls` transcripts,
    then runs one semantic search across all of them together.
    """
    page = await fetch_company_page(symbol, "consolidated")
    symbol, html = page.symbol, page.html

    reports = _parse_annual_reports(html) if include_annual_reports else []
    nse_warning = []
    if not reports and include_annual_reports:
        nse = await get_nse_client()
        try:
            reports = await nse.get_annual_reports(symbol)
        except NSEError as e:
            nse_warning = [f"NSE annual-report lookup failed ({e}) — annual reports may be missing."]
    calls = _parse_earnings_calls(html) if include_earnings_calls else []

    reports = sorted(reports, key=lambda r: r.get("year", ""), reverse=True)[:max_annual_reports]
    calls = calls[:max_earnings_calls]

    if not reports and not calls:
        raise ToolError(
            (
                f"**No documents found for {symbol}.**\n\n"
                f"Use `get_document_list('{symbol}')` to check what's available."
            ),
            "not_found",
        )

    collection_names: list[str] = []
    indexed_labels: list[str] = []
    errors: list[str] = []
    documents: list[dict] = []
    warnings: list[str] = list(page.warnings) + nse_warning

    for r in reports:
        name = f"{symbol}_{r.get('year', 'Unknown')}_annual"
        status = await process_document(
            r["url"], name,
            extra_metadata={"symbol": symbol, "doc_type": "annual_report", "label": str(r.get("year"))},
        )
        if status["status"] == "error":
            errors.append(f"Annual Report {r.get('year')}: {status.get('error')}")
            continue
        collection_names.append(name)
        indexed_labels.append(f"Annual Report {r.get('year')}")
        documents.append({"label": f"Annual Report {r.get('year')}", "url": r["url"], "freshness": status.get("freshness")})
        warnings += _freshness_warnings(f"Annual Report {r.get('year')}", status)

    for c in calls:
        quarter = c.get("quarter", "Unknown").upper().replace(" ", "")
        name = f"{symbol}_{quarter}_transcript"
        status = await process_document(
            c["url"], name,
            extra_metadata={"symbol": symbol, "doc_type": "earnings_call", "label": quarter},
        )
        if status["status"] == "error":
            errors.append(f"Earnings Call {quarter}: {status.get('error')}")
            continue
        collection_names.append(name)
        indexed_labels.append(f"Earnings Call {quarter}")
        documents.append({"label": f"Earnings Call {quarter}", "url": c["url"], "freshness": status.get("freshness")})
        warnings += _freshness_warnings(f"Earnings Call {quarter}", status)

    if not collection_names:
        raise ToolError(
            (
                f"**Failed to index any documents for {symbol}.**\n\n"
                + "\n".join(f"  - {e}" for e in errors)
            ),
            "document_error",
        )

    chunks = await query_documents(collection_names, question, top_k=8, top_k_per_collection=4)
    if not chunks:
        return f"**No relevant content found** for: '{question}'\n\nSearched: {', '.join(indexed_labels)}"

    context = "\n\n---\n\n".join(
        f"[Excerpt {i} — {c['metadata'].get('doc_type', '?')} "
        f"({c['metadata'].get('label', '?')}), Pages {c['metadata'].get('pages', '?')}, "
        f"relevance {c['score']}]\n{c['text']}"
        for i, c in enumerate(chunks, 1)
    )

    note = f"\n\n**Note:** could not index — {'; '.join(errors)}" if errors else ""

    report = f"""# Whole-Company Research — {symbol}

**Question:** {question}
**Documents searched:** {', '.join(indexed_labels)}{note}

## Relevant Excerpts (across all documents, ranked by relevance)

{context}

---
**Analyst task:** Using the excerpts above — which span multiple years/quarters —
answer: "{question}"

Structure your response as:
1. **Direct Answer** — synthesized across all sources found
2. **How it's changed over time** — if excerpts span multiple periods, note the trend
3. **Supporting Evidence** — cite which document/period each point comes from
4. **Caveats** — anything incomplete or that warrants checking the full document
"""
    return ToolResult(
        data={"report": report, "documents": documents},
        warnings=warnings + [f"Could not index {e}" for e in errors],
        partial=bool(errors),
        reason="Some documents could not be indexed." if errors else None,
    )


async def search_market_commentary(
    question: str,
    symbols: list[str],
    only_cached: bool = True,
    top_k_per_symbol: int = 3,
) -> str:
    """
    Semantic search for a question across MULTIPLE companies' cached documents
    at once — e.g. "which of these companies mentioned raw material cost
    pressure in their recent earnings calls?"

    By default (only_cached=True) this searches whatever has already been
    indexed via analyze_annual_report / analyze_earnings_call / ask_company_research
    for each symbol — it does not download new documents, to keep this fast
    and bounded regardless of how many symbols are passed. Run
    ask_company_research(symbol, ...) first for any symbol you want included
    that hasn't been indexed yet.
    """
    store = get_vector_store()
    symbols = [s.upper() for s in symbols]

    results_by_symbol: dict[str, list[dict]] = {}
    uncached_symbols: list[str] = []

    for sym in symbols:
        collections = store.list_collections(prefix=f"{sym}_")
        if not collections:
            uncached_symbols.append(sym)
            continue
        chunks = await query_documents(
            collections, question, top_k=top_k_per_symbol, top_k_per_collection=top_k_per_symbol
        )
        if chunks:
            results_by_symbol[sym] = chunks

    if not results_by_symbol:
        hint = (
            "\n\nNone of these symbols have indexed documents yet. Run "
            "`ask_company_research(symbol, question)` for each one first, "
            "then retry this search."
            if uncached_symbols == symbols
            else ""
        )
        return f"**No relevant content found** across {', '.join(symbols)}.{hint}"

    lines = ["# Cross-Company Search", "", f"**Question:** {question}", ""]

    for sym, chunks in results_by_symbol.items():
        lines.append(f"## {sym}")
        for c in chunks:
            meta = c["metadata"]
            lines.append(
                f"- [{meta.get('doc_type', '?')} {meta.get('label', '?')}, "
                f"pages {meta.get('pages', '?')}, relevance {c['score']}]"
            )
            lines.append(f"  {c['text'][:400]}{'...' if len(c['text']) > 400 else ''}")
        lines.append("")

    if uncached_symbols:
        lines.append(f"**Not yet indexed (skipped):** {', '.join(uncached_symbols)}")
        lines.append(
            "Run `ask_company_research(symbol, question)` for these to include them."
        )

    lines.append("")
    lines.append(
        f'**Analyst task:** Using the excerpts above, answer "{question}" '
        f"— compare/contrast across the companies that had relevant excerpts."
    )

    return "\n".join(lines)
