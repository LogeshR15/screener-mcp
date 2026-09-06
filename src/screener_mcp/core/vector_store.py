"""
ChromaDB vector store wrapper for semantic search over company documents.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PERSIST_DIR = Path.home() / ".screener-mcp" / "chroma_db"


class VectorStore:
    def __init__(self, persist_dir: Optional[str] = None):
        self.persist_dir = Path(
            persist_dir or os.getenv("CHROMA_PERSIST_DIR", str(_DEFAULT_PERSIST_DIR))
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import chromadb
            except ImportError:
                raise ImportError(
                    "chromadb not installed. Run: pip install chromadb\n"
                    "This is required for document analysis (analyze_annual_report, analyze_earnings_call)."
                )
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        return self._client

    def collection_exists(self, name: str) -> bool:
        try:
            client = self._get_client()
            existing = [c.name for c in client.list_collections()]
            return name in existing
        except Exception:
            return False

    def list_collections(self, prefix: str = "") -> list[str]:
        """List names of all persisted collections, optionally filtered by prefix."""
        try:
            client = self._get_client()
            names = [c.name for c in client.list_collections()]
            if prefix:
                names = [n for n in names if n.startswith(prefix)]
            return names
        except Exception:
            return []

    def get_or_create_collection(self, name: str):
        return self._get_client().get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"}
        )

    def add_documents(
        self,
        collection_name: str,
        docs: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        ids: list[str],
    ):
        col = self.get_or_create_collection(collection_name)
        col.add(documents=docs, embeddings=embeddings, metadatas=metadatas, ids=ids)

    def query(
        self, collection_name: str, query_embedding: list[float], top_k: int = 5
    ) -> list[dict]:
        col = self.get_or_create_collection(collection_name)
        count = col.count()
        if count == 0:
            return []
        results = col.query(
            query_embeddings=[query_embedding], n_results=min(top_k, count)
        )
        if not results["documents"]:
            return []
        chunks = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            chunks.append({"text": doc, "metadata": meta, "score": round(1 - dist, 4)})
        return chunks

    def delete_collection(self, name: str):
        try:
            self._get_client().delete_collection(name)
        except Exception:
            pass

    def count(self, collection_name: str) -> int:
        try:
            return self.get_or_create_collection(collection_name).count()
        except Exception:
            return 0


_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store
