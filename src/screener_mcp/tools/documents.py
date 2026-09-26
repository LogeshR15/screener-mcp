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
from ..core.rag import excerpt, process_document, query_documents
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


def _dedupe_reports(reports: list[dict]) -> list[dict]:
    """One report per year, preferring a PDF: Screener sometimes lists the same
    year twice (a .zip and a .pdf), and a .zip can't be indexed."""
    by_year: dict[str, dict] = {}
    for r in reports:
        r = {**r, "format": "zip" if r.get("url", "").lower().split("?")[0].endswith(".zip") else "pdf"}
        prev = by_year.get(str(r.get("year")))
        if prev is None or (prev["format"] != "pdf" and r["format"] == "pdf"):
            by_year[str(r.get("year"))] = r
    return sorted(by_year.values(), key=lambda x: str(x.get("year", "")), reverse=True)


async def _annual_reports(page, warnings: list[str]) -> list[dict]:
    reports = _parse_annual_reports(page.html)
    if not reports:
        try:
            reports = await (await get_nse_client()).get_annual_reports(page.symbol)
        except NSEError as e:
            warnings.append(f"NSE annual-report lookup failed ({e}) — the list may be incomplete.")
    return _dedupe_reports(reports)


async def get_document_list(symbol: str) -> ToolResult:
    """List all available annual reports and earnings call transcripts for a company."""
    page = await fetch_company_page(symbol, "consolidated")
    nse_warnings: list[str] = []
    reports = await _annual_reports(page, nse_warnings)
    warnings = list(page.warnings) + nse_warnings
    calls = _parse_earnings_calls(page.html)

    if not reports:
        warnings.append("No annual reports found via Screener.in or NSE — check the company's investor relations page.")
    if not calls:
        warnings.append("No earnings call transcripts listed on Screener.in.")
    if any(r["format"] == "zip" for r in reports):
        warnings.append("Some years are only available as .zip archives — those can't be searched; pass a PDF "
                        "link via pdf_url instead.")
    return ToolResult(
        data={
            "symbol": page.symbol,
            "annual_reports": [
                {"year": r.get("year"), "title": r.get("title"), "url": r.get("url"), "format": r["format"],
                 "source": r.get("source") or r.get("exchange")}
                for r in reports
            ],
            "earnings_calls": [
                {"quarter": c.get("quarter"), "title": c.get("title"), "url": c.get("url")} for c in calls
            ],
            "next_step": ("ask_company_research(symbol, question) searches them all; add year=2025 or "
                          "quarter='Q1FY26' for one document, or pdf_url for a link you already have."),
        },
        warnings=warnings,
        partial=bool(nse_warnings),
        reason=nse_warnings[0] if nse_warnings else None,
        meta=page.meta,
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


DOC_TYPES = ("all", "annual_report", "earnings_call")
_DEFAULT_QUESTIONS = {
    "annual_report": "Summarize the key business highlights, financial performance, risks, and management commentary.",
    "earnings_call": "What did management say about revenue, margins, business outlook, and key risks?",
    "all": "Summarize the business, recent performance, strategy, risks and management's outlook.",
}
EXCERPT_CHARS = 900


def _select_calls(calls: list[dict], quarter: str) -> list[dict]:
    q = quarter.upper().replace(" ", "")
    candidates = {q} | _quarter_to_month_labels(q)
    return [c for c in calls if any(cand in c.get("quarter", "").upper().replace(" ", "") for cand in candidates)]


async def _resolve_documents(page, doc_type: str, year: int, quarter: str, pdf_url: str,
                             max_annual_reports: int, max_earnings_calls: int,
                             warnings: list[str]) -> list[dict]:
    """→ [{doc_type, label, url}] to index for this request."""
    symbol = page.symbol
    if pdf_url:
        if doc_type == "earnings_call" or (quarter and not year):
            return [{"doc_type": "earnings_call", "label": quarter.upper().replace(" ", "") or "CUSTOM", "url": pdf_url}]
        return [{"doc_type": "annual_report", "label": str(year or "custom"), "url": pdf_url}]

    want_ar = doc_type in ("all", "annual_report") and not (quarter and not year)
    want_ec = doc_type in ("all", "earnings_call") and not (year and not quarter)
    docs = []
    if want_ar:
        reports = await _annual_reports(page, warnings)
        if year:
            reports = [r for r in reports if str(year) in str(r.get("year", ""))]
            if not reports:
                available = ", ".join(str(r.get("year")) for r in await _annual_reports(page, [])) or "none found"
                raise ToolError(f"No annual report for {symbol} {year}. Available years: {available}. "
                                "Pass pdf_url if you have the link.", "not_found")
        else:
            reports = reports[:max_annual_reports]
        for r in reports:
            if r["format"] == "zip":
                warnings.append(f"Annual Report {r.get('year')} is only available as a .zip — skipped.")
                continue
            docs.append({"doc_type": "annual_report", "label": str(r.get("year")), "url": r["url"]})
    if want_ec:
        calls = _parse_earnings_calls(page.html)
        if quarter:
            matched = _select_calls(calls, quarter)
            if not matched:
                available = ", ".join(c.get("quarter", "?") for c in calls) or "none on Screener.in"
                raise ToolError(f"No earnings call transcript for {symbol} {quarter}. Available: {available}. "
                                "Pass pdf_url if you have the link.", "not_found")
            calls = matched[:1]
        else:
            calls = calls[:max_earnings_calls]
        for c in calls:
            docs.append({"doc_type": "earnings_call", "label": c.get("quarter", "Unknown").upper().replace(" ", ""),
                         "url": c["url"]})
    return docs


def _collection(symbol: str, doc: dict) -> str:
    suffix = "annual" if doc["doc_type"] == "annual_report" else "transcript"
    return f"{symbol}_{doc['label']}_{suffix}"


def _doc_label(doc: dict) -> str:
    return f"{'Annual Report' if doc['doc_type'] == 'annual_report' else 'Earnings Call'} {doc['label']}"


async def ask_company_research(
    symbol: str,
    question: str = "",
    doc_type: str = "all",
    year: int = 0,
    quarter: str = "",
    pdf_url: str = "",
    max_annual_reports: int = 3,
    max_earnings_calls: int = 4,
    force_reindex: bool = False,
) -> ToolResult:
    """
    Semantic search over a company's annual reports and earnings-call
    transcripts — all recent ones at once, one type, or one document.
    """
    if doc_type not in DOC_TYPES:
        raise ToolError(f"doc_type must be one of {', '.join(DOC_TYPES)}.", "invalid_input")
    kind = "annual_report" if (year and not quarter) else "earnings_call" if (quarter and not year) else doc_type
    question = question.strip() or _DEFAULT_QUESTIONS[kind]

    page = await fetch_company_page(symbol, "consolidated")
    symbol = page.symbol
    warnings = list(page.warnings)
    docs = await _resolve_documents(page, doc_type, int(year or 0), quarter or "", pdf_url or "",
                                    max(1, int(max_annual_reports)), max(1, int(max_earnings_calls)), warnings)
    if not docs:
        raise ToolError(f"No documents found for {symbol}. Use get_document_list('{symbol}') to check what's "
                        "available, or pass pdf_url.", "not_found")

    names, documents, errors = [], [], []
    for doc in docs:
        name = _collection(symbol, doc)
        status = await process_document(
            doc["url"], name, force=force_reindex,
            extra_metadata={"symbol": symbol, "doc_type": doc["doc_type"], "label": doc["label"]},
        )
        if status["status"] == "error":
            errors.append(f"{_doc_label(doc)}: {status.get('error')}")
            continue
        names.append(name)
        documents.append({"label": _doc_label(doc), "url": doc["url"], "index": _source_info(status),
                          "freshness": status.get("freshness")})
        warnings += _freshness_warnings(_doc_label(doc), status)

    if not names:
        raise ToolError(
            f"Could not index any documents for {symbol}: " + "; ".join(errors)
            + ". Scanned/image-only PDFs have no text; otherwise check that the [ai] extra is installed "
              "(pip install 'screener-mcp[ai]').",
            "document_error",
        )

    top_k = 5 if len(names) == 1 else 8
    chunks = await query_documents(names, question, top_k=top_k, top_k_per_collection=4)
    excerpts = [
        {
            "document": f"{'Annual Report' if c['metadata'].get('doc_type') == 'annual_report' else 'Earnings Call'} "
                        f"{c['metadata'].get('label', '?')}",
            "pages": c["metadata"].get("pages", "?"),
            "relevance": c.get("rank_score", c["score"]),
            "text": excerpt(c["text"], question, EXCERPT_CHARS),
        }
        for c in chunks
    ]
    if not excerpts:
        warnings.append(f"The documents were indexed but nothing matched '{question}'. Try rephrasing.")

    context = "\n\n---\n\n".join(
        f"[{i}. {e['document']}, pages {e['pages']}, relevance {e['relevance']}]\n{e['text']}"
        for i, e in enumerate(excerpts, 1)
    )
    report = (
        f"# Company Research — {symbol}\n\n**Question:** {question}\n"
        f"**Searched:** {', '.join(d['label'] for d in documents)}\n\n## Relevant excerpts\n\n{context}\n\n"
        "---\nAnswer from these excerpts, citing the document and page for each point. Where excerpts span "
        "several periods, say how the picture changed. Flag anything the excerpts leave unanswered."
    )
    return ToolResult(
        data={"report": report, "question": question, "excerpts": excerpts, "documents": documents},
        warnings=warnings + [f"Could not index {e}" for e in errors],
        partial=bool(errors),
        reason="Some documents could not be indexed." if errors else None,
        meta=page.meta,
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
    indexed via ask_company_research for each symbol — it does not download new documents, to keep this fast
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
            lines.append(f"  {excerpt(c['text'], question, 400)}")
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
