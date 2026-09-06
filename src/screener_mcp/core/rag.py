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
import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from .vector_store import get_vector_store

logger = logging.getLogger(__name__)

_PDF_CACHE_DIR = Path.home() / ".screener-mcp" / "pdf_cache"
_CHUNK_WORDS = 500
_OVERLAP_WORDS = 60

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers not installed. Run: pip install sentence-transformers\n"
                "This is required for analyze_annual_report and analyze_earnings_call."
            )
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    return _get_embedder().encode(texts, convert_to_numpy=True).tolist()


def _embed_one(text: str) -> list[float]:
    return _embed([text])[0]


async def _download_pdf(url: str) -> bytes:
    _PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_path = _PDF_CACHE_DIR / f"{url_hash}.pdf"

    if cache_path.exists():
        logger.info(f"PDF cache hit: {url_hash}")
        return cache_path.read_bytes()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/pdf,*/*",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=90.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.content

    cache_path.write_bytes(data)
    return data


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
            text = page.extract_text() or ""
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 80:
                pages.append({"page": i + 1, "text": text})
    return pages


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
      {"status": "cached"|"processed"|"error", "chunks": N, "pages": N}
    """
    store = get_vector_store()

    if not force and store.collection_exists(collection_name):
        n = store.count(collection_name)
        return {"status": "cached", "chunks": n}

    try:
        pdf_bytes = await _download_pdf(url)

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

        return {"status": "processed", "chunks": len(chunks), "pages": len(pages)}

    except Exception as e:
        logger.error(f"Document processing error: {e}")
        return {"status": "error", "error": str(e)}


async def query_document(collection_name: str, question: str, top_k: int = 5) -> list[dict]:
    """Semantic search — returns top-k relevant chunks for a question."""
    store = get_vector_store()
    if not store.collection_exists(collection_name):
        return []

    loop = asyncio.get_event_loop()
    q_embedding = await loop.run_in_executor(None, _embed_one, question)
    return store.query(collection_name, q_embedding, top_k=top_k)


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
        chunks = store.query(name, q_embedding, top_k=top_k_per_collection)
        for c in chunks:
            c["collection"] = name
        all_chunks.extend(chunks)

    all_chunks.sort(key=lambda c: c["score"], reverse=True)
    return all_chunks[:top_k]
