"""Retrieve cross-file context for a PR diff."""
from __future__ import annotations

from codereview.context.embedding_cache import EmbeddingService
from codereview.context.index import CHUNKER_VERSION
from codereview.context.store import SearchHit, VectorStore, collection_name


class ContextRetriever:
    def __init__(
        self,
        embeddings: EmbeddingService | None,
        store: VectorStore,
        *,
        max_chars: int = 12_000,
    ):
        self.embeddings = embeddings
        self.store = store
        self.max_chars = max_chars

    def available(self, repo: str, ref: str) -> bool:
        return bool(
            self.embeddings
            and ref
            and self.store.has(collection_name(repo, ref, CHUNKER_VERSION))
        )

    def retrieve(
        self,
        repo: str,
        ref: str,
        query: str,
        *,
        k: int = 12,
        exclude_paths: set[str] | None = None,
    ) -> list[SearchHit]:
        if not self.embeddings or not ref:
            return []
        collection = collection_name(repo, ref, CHUNKER_VERSION)
        if not self.store.has(collection):
            return []
        vector = self.embeddings.embed_query(query[:12_000])
        return self.store.query(collection, vector, k=k, exclude_paths=exclude_paths or set())

    def context_for_diff(
        self,
        repo: str,
        ref: str,
        title: str,
        diff: str,
        *,
        k: int = 12,
        exclude_paths: set[str] | None = None,
    ) -> str:
        query = f"{title}\n\n{diff[:6000]}"
        hits = self.retrieve(repo, ref, query, k=k, exclude_paths=exclude_paths)
        return self.render(hits)

    def render(self, hits: list[SearchHit]) -> str:
        parts: list[str] = []
        used = 0
        for hit in hits:
            metadata = hit.metadata or {}
            header = (
                f"# {metadata.get('file_path', 'unknown')}:"
                f"{metadata.get('name', '')} "
                f"(L{metadata.get('line_start', '?')}-L{metadata.get('line_end', '?')})"
            )
            text = hit.text
            body = text.split("\n\n", 1)[1] if "\n\n" in text else text
            block = f"{header}\n{body}"
            if used + len(block) > self.max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)
