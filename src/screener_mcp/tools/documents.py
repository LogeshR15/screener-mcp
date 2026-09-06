"""
Document analysis tools — annual reports and earnings call transcripts.

Fetches document links from Screener.in, downloads PDFs, and answers
questions via semantic search (RAG: chromadb + sentence-transformers).
"""

import logging
import re

from bs4 import BeautifulSoup

from ..client import get_client
from ..core.nse_client import get_nse_client
from ..core.rag import process_document, query_document, query_documents
from ..core.vector_store import get_vector_store

logger = logging.getLogger(__name__)


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
            if not (href.endswith(".pdf") or "annual" in href.lower() or "annual" in text.lower()):
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
        for a in soup.find_all("a", href=re.compile(r"annual.?report|AnnualReport", re.I)):
            href = a["href"]
            text = re.sub(r"\s+", " ", a.get_text()).strip()
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
    """Extract earnings call transcript links from the Screener.in company page."""
    soup = BeautifulSoup(html, "lxml")
    transcripts = []

    for section_id in ["earning-calls-transcripts", "transcripts", "concalls", "investor-presentations"]:
        section = soup.find(id=section_id)
        if not section:
            continue
        for a in section.find_all("a", href=True):
            href = a["href"]
            text = re.sub(r"\s+", " ", a.get_text()).strip()
            quarter = re.search(r"Q[1-4]\s*FY?\s*\d{2,4}", text, re.I)
            url = href if href.startswith("http") else f"https://www.screener.in{href}"
            transcripts.append({
                "quarter": quarter.group().upper().replace(" ", "") if quarter else text[:30],
                "title": text or "Earnings Call Transcript",
                "url": url,
                "type": "earnings_call",
                "source": "screener",
            })
        break

    return transcripts


async def get_document_list(symbol: str) -> str:
    """List all available annual reports and earnings call transcripts for a company."""
    client = await get_client()
    nse = await get_nse_client()

    html = await client.get_html(f"/company/{symbol.upper()}/consolidated/")
    screener_reports = _parse_annual_reports(html)
    screener_calls = _parse_earnings_calls(html)

    nse_reports = []
    if not screener_reports:
        nse_reports = await nse.get_annual_reports(symbol)

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

    return "\n".join(lines)


async def analyze_annual_report(
    symbol: str,
    year: int,
    question: str,
    pdf_url: str = None,
) -> str:
    """
    Ask any question about a company's annual report using semantic search over the PDF.

    If pdf_url is not given, fetches it automatically via Screener.in / NSE.
    """
    if not question.strip():
        question = "Summarize the key business highlights, financial performance, risks, and management commentary from this annual report."

    if not pdf_url:
        client = await get_client()
        html = await client.get_html(f"/company/{symbol.upper()}/consolidated/")
        reports = _parse_annual_reports(html)

        if not reports:
            nse = await get_nse_client()
            reports = await nse.get_annual_reports(symbol)

        matched = [r for r in reports if str(year) in str(r.get("year", ""))]
        if not matched:
            available = sorted({r.get("year") for r in reports}, reverse=True)
            return (
                f"**Annual report for {symbol.upper()} ({year}) not found.**\n\n"
                f"Available years: {', '.join(str(y) for y in available) or 'None found'}\n\n"
                f"Use `get_document_list('{symbol}')` to see what's available, or pass `pdf_url` directly."
            )
        pdf_url = matched[0]["url"]

    collection_name = f"{symbol.upper()}_{year}_annual"

    status = await process_document(
        pdf_url,
        collection_name,
        extra_metadata={"symbol": symbol.upper(), "doc_type": "annual_report", "label": str(year)},
    )
    if status["status"] == "error":
        return (
            f"**Failed to process annual report PDF.**\n\n"
            f"Error: {status.get('error')}\n"
            f"URL: {pdf_url}\n\n"
            f"Common causes:\n"
            f"  - PDF is scanned/image-only (no machine-readable text)\n"
            f"  - URL requires authentication\n"
            f"  - pdfplumber or sentence-transformers not installed\n\n"
            f"Run: `pip install pdfplumber sentence-transformers chromadb`"
        )

    chunks = await query_document(collection_name, question, top_k=5)
    if not chunks:
        return f"**No relevant content found** for: '{question}'\n\nThe document was indexed but no matching sections were found. Try rephrasing your question."

    context = "\n\n---\n\n".join(
        f"[Excerpt {i} — Pages {c['metadata'].get('pages', '?')}]\n{c['text']}"
        for i, c in enumerate(chunks, 1)
    )
    source_info = (
        f"cached ({status['chunks']} chunks)"
        if status["status"] == "cached"
        else f"freshly indexed ({status['chunks']} chunks across {status.get('pages', '?')} pages)"
    )

    return f"""# Annual Report Analysis — {symbol.upper()} ({year})

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


async def analyze_earnings_call(
    symbol: str,
    quarter: str,
    question: str,
    pdf_url: str = None,
) -> str:
    """
    Ask any question about an earnings call transcript.

    quarter: e.g., "Q1FY25", "Q2FY26", "Q3FY25"
    """
    if not question.strip():
        question = "What did management say about revenue, margins, business outlook, and key risks?"

    quarter_clean = quarter.upper().replace(" ", "")

    if not pdf_url:
        client = await get_client()
        html = await client.get_html(f"/company/{symbol.upper()}/consolidated/")
        calls = _parse_earnings_calls(html)

        matched = [
            c for c in calls
            if quarter_clean in c.get("quarter", "").upper().replace(" ", "")
        ]
        if not matched and calls:
            available = [c.get("quarter") for c in calls]
            return (
                f"**Earnings call for {symbol.upper()} ({quarter}) not found.**\n\n"
                f"Available quarters: {', '.join(available)}\n\n"
                f"Use `get_document_list('{symbol}')` to see all transcripts, or pass `pdf_url` directly."
            )
        if not matched:
            return (
                f"**No earnings call transcripts found for {symbol.upper()} on Screener.in.**\n\n"
                f"You can pass the PDF URL directly via `pdf_url` parameter."
            )
        pdf_url = matched[0]["url"]

    collection_name = f"{symbol.upper()}_{quarter_clean}_transcript"

    status = await process_document(
        pdf_url,
        collection_name,
        extra_metadata={"symbol": symbol.upper(), "doc_type": "earnings_call", "label": quarter_clean},
    )
    if status["status"] == "error":
        return (
            f"**Failed to process earnings call transcript.**\n\n"
            f"Error: {status.get('error')}\n"
            f"URL: {pdf_url}\n\n"
            f"Run: `pip install pdfplumber sentence-transformers chromadb`"
        )

    chunks = await query_document(collection_name, question, top_k=5)
    if not chunks:
        return f"**No relevant content found** for: '{question}'"

    context = "\n\n---\n\n".join(
        f"[Excerpt {i} — Pages {c['metadata'].get('pages', '?')}]\n{c['text']}"
        for i, c in enumerate(chunks, 1)
    )
    source_info = "cached" if status["status"] == "cached" else f"freshly indexed ({status['chunks']} chunks)"

    return f"""# Earnings Call Analysis — {symbol.upper()} ({quarter})

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
    symbol = symbol.upper()
    client = await get_client()
    html = await client.get_html(f"/company/{symbol}/consolidated/")

    reports = _parse_annual_reports(html) if include_annual_reports else []
    if not reports and include_annual_reports:
        nse = await get_nse_client()
        reports = await nse.get_annual_reports(symbol)
    calls = _parse_earnings_calls(html) if include_earnings_calls else []

    reports = sorted(reports, key=lambda r: r.get("year", ""), reverse=True)[:max_annual_reports]
    calls = calls[:max_earnings_calls]

    if not reports and not calls:
        return (
            f"**No documents found for {symbol}.**\n\n"
            f"Use `get_document_list('{symbol}')` to check what's available."
        )

    collection_names: list[str] = []
    indexed_labels: list[str] = []
    errors: list[str] = []

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

    if not collection_names:
        return (
            f"**Failed to index any documents for {symbol}.**\n\n"
            + "\n".join(f"  - {e}" for e in errors)
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

    return f"""# Whole-Company Research — {symbol}

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
            f"\n\nNone of these symbols have indexed documents yet. Run "
            f"`ask_company_research(symbol, question)` for each one first, "
            f"then retry this search."
            if uncached_symbols == symbols
            else ""
        )
        return f"**No relevant content found** across {', '.join(symbols)}.{hint}"

    lines = [f"# Cross-Company Search", "", f"**Question:** {question}", ""]

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
