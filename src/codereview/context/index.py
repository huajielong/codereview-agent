"""Incremental repository index.

Fast path: ``(repo, ref)`` was indexed and the vector collection still exists.
Slow path: parse only files whose chunk cache misses, then embed only texts
whose content hash misses. Chroma is rebuilt from cached vectors, which is
cheap compared with re-embedding or re-downloading the repository.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from codereview.context.chunker import Chunk, blob_sha, chunk_file, iter_repo_files
from codereview.context.embedding_cache import EmbeddingService
from codereview.context.store import VectorRecord, VectorStore, collection_name
from codereview.errors import IndexError_
from codereview.models import IndexStats
from codereview.storage.db import Database

CHUNKER_VERSION = "v1"


def _record_id(path: str, chunk: Chunk) -> str:
    blob = f"{path}:{chunk.name}:{chunk.line_start}:{chunk.content}"
    return hashlib.sha1(blob.encode("utf-8", errors="replace")).hexdigest()[:24]


class RepoIndexer:
    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingService | None,
        store: VectorStore,
        *,
        max_file_bytes: int = 200_000,
        max_chunk_chars: int = 8000,
    ):
        self.db = db
        self.embeddings = embeddings
        self.store = store
        self.max_file_bytes = max_file_bytes
        self.max_chunk_chars = max_chunk_chars

    def ensure_index(self, repo: str, ref: str, root: str | Path) -> IndexStats:
        if not ref:
            raise IndexError_("ref is required to build an index")
        if self.embeddings is None:
            raise IndexError_("embedding channel is not configured")

        root_path = Path(root)
        if not root_path.exists():
            raise IndexError_(f"workspace does not exist: {root_path}")
        collection = collection_name(repo, ref, CHUNKER_VERSION)

        started = time.monotonic()
        state = self.db.index_state_get(repo, ref)
        if state and self.store.persistent and self.store.has(collection):
            return IndexStats(
                repo=repo,
                ref=ref,
                files=int(state["file_count"]),
                chunks=int(state["chunk_count"]),
                skipped=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        records: list[VectorRecord] = []
        new_entries: list[tuple[str, str, str, list[dict]]] = []
        reused_files = 0
        file_count = 0

        for path, source in iter_repo_files(root_path, self.max_file_bytes):
            file_count += 1
            digest = f"{blob_sha(source)}:{CHUNKER_VERSION}"
            cached = self.db.chunk_get(repo, path, digest)
            if cached is None:
                chunks = chunk_file(source, path)
                cached = [chunk.to_dict() for chunk in chunks]
                new_entries.append((repo, path, digest, cached))
            else:
                reused_files += 1

            for data in cached:
                chunk = Chunk.from_dict(data)
                text = f"{chunk.type}: {chunk.name}\n\n{chunk.content}"
                records.append(
                    VectorRecord(
                        id=_record_id(path, chunk),
                        text=text,
                        vector=[],
                        metadata={
                            "file_path": path,
                            "name": chunk.name,
                            "type": chunk.type,
                            "line_start": int(chunk.line_start),
                            "line_end": int(chunk.line_end),
                            "language": chunk.language,
                        },
                    )
                )

        if new_entries:
            self.db.chunk_put_many(new_entries)

        embedded = 0
        if records:
            vectors = self.embeddings.embed_documents([record.text for record in records])
            if len(vectors) != len(records):
                raise IndexError_(
                    f"embedding count mismatch: {len(vectors)} vectors / {len(records)} chunks"
                )
            for record, vector in zip(records, vectors):
                record.vector = vector
            embedded = self.embeddings.last_misses

        self.store.replace(collection, records)
        self.db.index_state_set(repo, ref, file_count, len(records))

        return IndexStats(
            repo=repo,
            ref=ref,
            files=file_count,
            chunks=len(records),
            reused_files=reused_files,
            embedded_chunks=embedded,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
