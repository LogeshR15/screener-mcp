"""
RAG pipeline for company document analysis.

Flow:
  1. Download PDF (with disk cache)
  2. Parse pages with pdfplumber
  3. Chunk into overlapping word segments
  4. Embed with sentence-transformers (all-MiniLM-L6-v2, ~80 MB, runs locally)
  5. Store in ChromaDB
  6. Query: embed question → nearest-neighbour search → return top-k chunks
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .vector_store import get_vector_store

logger = logging.getLogger(__name__)

_PDF_CACHE_DIR = Path.home() / ".screener-mcp" / "pdf_cache"
# collection name → {source_url, last_indexed_at, content_sha256, etag, last_modified, content_length}
_MANIFEST_PATH = Path.home() / ".screener-mcp" / "index_manifest.json"
_CHUNK_WORDS = 500
_OVERLAP_WORDS = 60
# Bump when text extraction or chunking changes: cached indexes built by an
# older version are rebuilt on next use (the PDF itself stays disk-cached).
# v2: column-aware extraction, mirrored/rotated text dropped.
INDEX_VERSION = 2

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers not installed. Run: pip install sentence-transformers\n"
                "This is required for ask_company_research and search_market_commentary."
            )
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    return _get_embedder().encode(texts, convert_to_numpy=True).tolist()


def _embed_one(text: str) -> list[float]:
    return _embed([text])[0]


def _load_manifest() -> dict:
    try:
        return json.loads(_MANIFEST_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_manifest_entry(collection_name: str, entry: dict):
    manifest = _load_manifest()
    manifest[collection_name] = entry
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def get_index_info(collection_name: str) -> Optional[dict]:
    return _load_manifest().get(collection_name)


async def _download_pdf(url: str, force: bool = False) -> tuple[bytes, dict]:
    """Return (pdf bytes, response validators). Disk-cached by URL unless force."""
    _PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_path = _PDF_CACHE_DIR / f"{url_hash}.pdf"

    if cache_path.exists() and not force:
        logger.info(f"PDF cache hit: {url_hash}")
        return cache_path.read_bytes(), {}

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/pdf,*/*",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=90.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.content
        validators = _validators(resp.headers)

    cache_path.write_bytes(data)
    return data, validators


def _validators(headers) -> dict:
    return {
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
        "content_length": headers.get("Content-Length"),
    }


async def check_source_changed(url: str, info: dict) -> Optional[bool]:
    """HEAD the source and compare ETag / Last-Modified / Content-Length with
    what was recorded at index time. None = can't tell (no validators, HEAD
    unsupported, network error)."""
    recorded = {k: info.get(k) for k in ("etag", "last_modified", "content_length") if info.get(k)}
    if not recorded:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            resp = await client.head(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return None
        current = _validators(resp.headers)
    except httpx.HTTPError:
        return None
    compared = [(k, v) for k, v in recorded.items() if current.get(k)]
    if not compared:
        return None
    return any(current[k] != v for k, v in compared)


def _age_hours(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600, 1)
    except ValueError:
        return None


async def freshness(collection_name: str, url: Optional[str], check_source: bool = True) -> dict:
    """Freshness block for a cached index: when it was built, from what, and
    whether the source looks different now."""
    info = get_index_info(collection_name)
    if not info:
        return {
            "last_indexed_at": None,
            "content_sha256": None,
            "source_changed": None,
            "note": "Indexed before freshness tracking existed — age unknown. Pass force_reindex=True to rebuild.",
        }
    out = {
        "last_indexed_at": info.get("last_indexed_at"),
        "age_hours": _age_hours(info.get("last_indexed_at")),
        "source_url": info.get("source_url"),
        "content_sha256": info.get("content_sha256"),
        "etag": info.get("etag"),
        "last_modified": info.get("last_modified"),
        "source_changed": None,
    }
    if url and info.get("source_url") and url != info.get("source_url"):
        out["source_changed"] = True
        out["note"] = "The document link differs from the one that was indexed."
    elif check_source and (url or info.get("source_url")):
        out["source_changed"] = await check_source_changed(url or info["source_url"], info)
    return out


def _parse_pdf_sync(pdf_bytes: bytes) -> list[dict]:
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber not installed. Run: pip install pdfplumber\n"
            "This is required for PDF document analysis."
        )
    import io

    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = _page_text(page)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 80:
                pages.append({"page": i + 1, "text": text})
    return pages


def _readable_char(obj: dict) -> bool:
    """Drop rotated and mirrored glyphs. Annual-report covers and section
    dividers often carry decorative text set sideways or flipped, which
    pdfplumber would otherwise emit reversed ("GNIYFITROF" for FORTIFYING)."""
    if obj.get("object_type") != "char":
        return True
    matrix = obj.get("matrix") or (1, 0, 0, 1)
    return bool(obj.get("upright", True)) and matrix[0] > 0 and matrix[3] > 0


def column_gutter(words: list[dict], width: float) -> Optional[float]:
    """x position of the gap between two text columns, or None for a
    single-column page.

    A gutter is an x in the middle 30-70% of the page that almost no word
    crosses (full-width headings may), with a real share of words on each
    side. Without splitting there, pdfplumber reads straight across both
    columns and interleaves their lines.
    """
    if len(words) < 40 or width <= 0:
        return None
    w = int(width) + 1
    coverage = [0] * w
    for wd in words:
        for x in range(max(0, int(wd["x0"])), min(w, int(wd["x1"]) + 1)):
            coverage[x] += 1
    lo, hi, mid = int(0.3 * width), int(0.7 * width), width / 2
    allowed = max(1, int(0.03 * len(words)))
    best = None
    for x in range(lo, hi):
        if coverage[x] > allowed:
            continue
        left = sum(1 for wd in words if wd["x1"] <= x)
        right = sum(1 for wd in words if wd["x0"] >= x)
        if min(left, right) < 0.2 * len(words):
            continue
        key = (coverage[x], abs(x - mid))
        if best is None or key < best[0]:
            best = (key, x)
    return float(best[1]) if best else None


def _page_text(page) -> str:
    page = page.filter(_readable_char)
    try:
        words = page.extract_words()
    except Exception:
        words = []
    gutter = column_gutter(words, float(page.width))
    if gutter is None:
        return page.extract_text() or ""
    x0, top, x1, bottom = page.bbox
    left = page.crop((x0, top, x0 + gutter, bottom)).extract_text() or ""
    right = page.crop((x0 + gutter, top, x1, bottom)).extract_text() or ""
    return f"{left}\n{right}"


def _chunk_pages(pages: list[dict]) -> list[dict]:
    """Split pages into overlapping word-count chunks, tagging with page numbers."""
    all_words: list[str] = []
    word_page: list[int] = []

    for page in pages:
        words = page["text"].split()
        all_words.extend(words)
        word_page.extend([page["page"]] * len(words))

    chunks = []
    i = 0
    idx = 0
    while i < len(all_words):
        end = min(i + _CHUNK_WORDS, len(all_words))
        chunk_words = all_words[i:end]
        chunk_pages = sorted(set(word_page[i:end]))
        text = " ".join(chunk_words).strip()
        if text:
            chunks.append({
                "text": text,
                "chunk_idx": idx,
                "pages": chunk_pages,
                "page_start": min(chunk_pages),
            })
            idx += 1
        i += _CHUNK_WORDS - _OVERLAP_WORDS

    return chunks


async def process_document(
    url: str,
    collection_name: str,
    force: bool = False,
    extra_metadata: Optional[dict] = None,
) -> dict:
    """
    Download, parse, embed, and index a PDF document.

    extra_metadata: merged into every chunk's metadata (e.g. symbol, doc_type,
    label) so results can be traced back to their source document when
    searching across multiple documents at once.

    Returns:
      {"status": "cached"|"processed"|"error", "chunks": N, "pages": N,
       "freshness": {last_indexed_at, content_sha256, source_changed, ...}}

    force=True re-downloads the PDF (bypassing the disk cache) and rebuilds the index.
    """
    store = get_vector_store()

    info = get_index_info(collection_name) or {}
    current = info.get("index_version") == INDEX_VERSION
    if not force and current and store.collection_exists(collection_name):
        n = store.count(collection_name)
        return {"status": "cached", "chunks": n, "freshness": await freshness(collection_name, url)}

    try:
        # A stale-version rebuild reuses the disk-cached PDF; only force re-downloads.
        pdf_bytes, validators = await _download_pdf(url, force=force)

        loop = asyncio.get_event_loop()
        pages = await loop.run_in_executor(None, _parse_pdf_sync, pdf_bytes)
        if not pages:
            return {"status": "error", "error": "No readable text found in PDF (may be scanned/image-only)"}

        chunks = _chunk_pages(pages)
        if not chunks:
            return {"status": "error", "error": "Text extracted but produced no chunks"}

        texts = [c["text"] for c in chunks]
        embeddings = await loop.run_in_executor(None, _embed, texts)

        ids = [f"{collection_name}_{c['chunk_idx']}" for c in chunks]
        metadatas = [
            {
                "pages": str(c["pages"]),
                "page_start": c["page_start"],
                **(extra_metadata or {}),
            }
            for c in chunks
        ]

        store.delete_collection(collection_name)
        store.add_documents(collection_name, texts, embeddings, metadatas, ids)

        entry = {
            "index_version": INDEX_VERSION,
            "source_url": url,
            "last_indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "content_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            **validators,
        }
        _save_manifest_entry(collection_name, entry)

        return {
            "status": "processed",
            "chunks": len(chunks),
            "pages": len(pages),
            "freshness": {**entry, "age_hours": 0.0, "source_changed": False},
        }

    except Exception as e:
        logger.error(f"Document processing error: {e}")
        return {"status": "error", "error": str(e)}


# BRSR / sustainability-report boilerplate matches almost any "risk" or
# "strategy" question semantically and crowded out the business discussion.
_BOILERPLATE_RE = re.compile(
    r"\b(brsr|business responsibility|essential indicators?|leadership indicators?|principle \d|"
    r"sustainability report|scope [123] emissions|material issues?|risk/opportunity|\(r/o\))", re.I)
_BOILERPLATE_OK_RE = re.compile(r"brsr|esg|sustainab|business responsibility|csr|emission|climate", re.I)
_STOPWORDS = set("""a an and are as at be been by did do does for from has have how in is it its of on or
that the their this to was were what when where which who why will with about over management company""".split())
BOILERPLATE_PENALTY = 0.15
KEYWORD_WEIGHT = 0.1


def _terms(text: str) -> set[str]:
    """Content words, plurals folded ("risks" → "risk") so they match."""
    words = (w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOPWORDS)
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def rerank(chunks: list[dict], question: str, top_k: int) -> list[dict]:
    """Re-order vector hits: penalise BRSR boilerplate (unless the question is
    about ESG), reward chunks that contain the question's words, and keep
    at most one chunk per ~3-page window of a document so the answer draws on
    several sections instead of three overlapping chunks of one."""
    q_terms = _terms(question)
    penalise = not _BOILERPLATE_OK_RE.search(question)
    for c in chunks:
        score = c["score"]
        if penalise and len(_BOILERPLATE_RE.findall(c["text"])) >= 2:
            score -= BOILERPLATE_PENALTY
        if q_terms:
            score += KEYWORD_WEIGHT * len(q_terms & _terms(c["text"])) / len(q_terms)
        c["rank_score"] = round(score, 4)
    chunks.sort(key=lambda c: c["rank_score"], reverse=True)

    chosen: list[dict] = []
    for c in chunks:
        start = c.get("metadata", {}).get("page_start")
        coll = c.get("collection")
        if isinstance(start, int) and any(
            o.get("collection") == coll and isinstance(o["metadata"].get("page_start"), int)
            and abs(o["metadata"]["page_start"] - start) <= 2 for o in chosen
        ):
            continue
        chosen.append(c)
        if len(chosen) == top_k:
            break
    return chosen


def excerpt(text: str, question: str, max_chars: int = 900) -> str:
    """The ``max_chars`` window of a chunk that holds the most question terms,
    cut on sentence boundaries where possible. Whole chunks (~3,000 chars)
    made multi-document answers enormous."""
    if len(text) <= max_chars:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    q_terms = _terms(question)
    best, best_score = (0, 1, sentences[0][:max_chars]), -1
    for i in range(len(sentences)):
        window, j = "", i
        while j < len(sentences) and len(window) + len(sentences[j]) + 1 <= max_chars:
            window = f"{window} {sentences[j]}".strip()
            j += 1
        if not window:  # a single sentence longer than max_chars
            window, j = sentences[i][:max_chars], i + 1
        score = len(q_terms & _terms(window))
        if score > best_score:
            best, best_score = (i, j, window), score
    i, j, window = best
    return f"{'… ' if i > 0 else ''}{window}{' …' if j < len(sentences) else ''}"


async def query_document(collection_name: str, question: str, top_k: int = 5) -> list[dict]:
    """Semantic search — returns top-k relevant chunks for a question."""
    store = get_vector_store()
    if not store.collection_exists(collection_name):
        return []

    loop = asyncio.get_event_loop()
    q_embedding = await loop.run_in_executor(None, _embed_one, question)
    chunks = store.query(collection_name, q_embedding, top_k=top_k * 3)
    for c in chunks:
        c["collection"] = collection_name
    return rerank(chunks, question, top_k)


async def query_documents(
    collection_names: list[str], question: str, top_k: int = 5, top_k_per_collection: int = 5
) -> list[dict]:
    """
    Semantic search across multiple document collections at once, merging
    results by score. Each returned chunk's metadata carries whatever
    extra_metadata (symbol, doc_type, label) was set when it was indexed,
    so the caller can attribute each excerpt to its source document.
    """
    store = get_vector_store()
    loop = asyncio.get_event_loop()
    q_embedding = await loop.run_in_executor(None, _embed_one, question)

    all_chunks: list[dict] = []
    for name in collection_names:
        if not store.collection_exists(name):
            continue
        chunks = store.query(name, q_embedding, top_k=top_k_per_collection * 3)
        for c in chunks:
            c["collection"] = name
        all_chunks.extend(chunks)

    return rerank(all_chunks, question, top_k)
