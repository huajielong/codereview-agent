"""Retrieve cross-file context for a PR diff."""
from __future__ import annotations

from codereview.context.embedding_cache import EmbeddingService
from codereview.context.index import CHUNKER_VERSION
from codereview.context.languages import expand_languages, retrieval_groups
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
        languages: set[str] | None = None,
    ) -> list[SearchHit]:
        if not self.embeddings or not ref:
            return []
        collection = collection_name(repo, ref, CHUNKER_VERSION)
        if not self.store.has(collection):
            return []
        vector = self.embeddings.embed_query(query[:12_000])
        excluded = exclude_paths or set()
        if languages:
            groups = retrieval_groups(languages)
            if len(groups) > 1:
                # Reserve a slot per language family so one language cannot
                # crowd out the others in a mixed PR.
                per_group = max(1, k // len(groups))
                hits: list[SearchHit] = []
                seen: set[str] = set()
                for group in groups:
                    for hit in self.store.query(
                        collection,
                        vector,
                        k=per_group,
                        exclude_paths=excluded,
                        languages=group,
                    ):
                        if hit.id not in seen:
                            hits.append(hit)
                            seen.add(hit.id)
            else:
                hits = self.store.query(
                    collection,
                    vector,
                    k=k,
                    exclude_paths=excluded,
                    languages=set(expand_languages(languages)),
                )
                seen = {hit.id for hit in hits}
            if len(hits) < k:
                # Same-language chunks are a preference, not a hard filter.
                for hit in self.store.query(
                    collection, vector, k=k, exclude_paths=excluded, languages=None
                ):
                    if hit.id in seen:
                        continue
                    hits.append(hit)
                    seen.add(hit.id)
                    if len(hits) >= k:
                        break
            return hits[:k]
        return self.store.query(
            collection, vector, k=k, exclude_paths=excluded, languages=None
        )

    def context_for_diff(
        self,
        repo: str,
        ref: str,
        title: str,
        diff: str,
        *,
        k: int = 12,
        exclude_paths: set[str] | None = None,
        languages: set[str] | None = None,
    ) -> str:
        query = f"{title}\n\n{diff[:6000]}"
        hits = self.retrieve(
            repo,
            ref,
            query,
            k=k,
            exclude_paths=exclude_paths,
            languages=languages,
        )
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
            remaining = self.max_chars - used
            if remaining <= 0:
                break
            if len(block) > remaining:
                # A single AST class can exceed max_chars. Returning an empty
                # context because the top hit is large silently disables
                # retrieval; keep a truncated first block instead.
                marker = "\n...[truncated]"
                if remaining <= len(marker):
                    break
                parts.append(block[: remaining - len(marker)] + marker)
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)
