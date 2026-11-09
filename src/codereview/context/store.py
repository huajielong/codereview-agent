"""Vector store abstraction: persistent Chroma for production, memory for tests."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class VectorRecord:
    id: str
    text: str
    vector: list[float]
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchHit:
    id: str
    text: str
    metadata: dict
    score: float


def collection_name(repo: str, ref: str, version: str = "v1") -> str:
    # The chunker version is part of the collection key: a chunking change must
    # not silently reuse vectors built from the old chunk boundaries.
    digest = hashlib.sha1(f"{repo}@{ref}:{version}".encode("utf-8")).hexdigest()[:24]
    return f"cr_{digest}"


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


class VectorStore(Protocol):
    persistent: bool

    def has(self, collection: str) -> bool:
        ...

    def replace(self, collection: str, records: list[VectorRecord]) -> None:
        ...

    def drop(self, collection: str) -> None:
        ...

    def query(
        self, collection: str, vector: list[float], k: int, exclude_paths: set[str] | None = None
    ) -> list[SearchHit]:
        ...


class MemoryVectorStore:
    persistent = False

    def __init__(self):
        self._collections: dict[str, list[VectorRecord]] = {}

    def has(self, collection: str) -> bool:
        return collection in self._collections

    def replace(self, collection: str, records: list[VectorRecord]) -> None:
        self._collections[collection] = list(records)

    def drop(self, collection: str) -> None:
        self._collections.pop(collection, None)

    def query(
        self, collection: str, vector: list[float], k: int, exclude_paths: set[str] | None = None
    ) -> list[SearchHit]:
        exclude = exclude_paths or set()
        hits = []
        for record in self._collections.get(collection, []):
            if record.metadata.get("file_path") in exclude:
                continue
            hits.append(
                SearchHit(
                    id=record.id,
                    text=record.text,
                    metadata=record.metadata,
                    score=cosine_similarity(vector, record.vector),
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:k]


class ChromaVectorStore:
    persistent = True

    def __init__(self, path: str):
        import chromadb

        self._client = chromadb.PersistentClient(path=str(path))

    def has(self, collection: str) -> bool:
        try:
            self._client.get_collection(collection)
            return True
        except Exception:  # noqa: BLE001 - chroma raises several types
            return False

    def replace(self, collection: str, records: list[VectorRecord]) -> None:
        try:
            self._client.delete_collection(collection)
        except Exception:  # noqa: BLE001
            pass
        if not records:
            return
        col = self._client.create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )
        for start in range(0, len(records), 500):
            batch = records[start:start + 500]
            col.add(
                ids=[record.id for record in batch],
                embeddings=[record.vector for record in batch],
                documents=[record.text for record in batch],
                metadatas=[record.metadata for record in batch],
            )

    def drop(self, collection: str) -> None:
        try:
            self._client.delete_collection(collection)
        except Exception:  # noqa: BLE001
            pass

    def query(
        self, collection: str, vector: list[float], k: int, exclude_paths: set[str] | None = None
    ) -> list[SearchHit]:
        exclude = exclude_paths or set()
        col = self._client.get_collection(collection)
        n = max(k * 3, k + len(exclude), 10)
        results = col.query(
            query_embeddings=[vector],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )
        ids = (results.get("ids") or [[]])[0]
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        hits: list[SearchHit] = []
        for index, item_id in enumerate(ids):
            metadata = metadatas[index] or {}
            if metadata.get("file_path") in exclude:
                continue
            distance = distances[index] if index < len(distances) else 1.0
            hits.append(
                SearchHit(
                    id=item_id,
                    text=documents[index] or "",
                    metadata=metadata,
                    score=1.0 - float(distance),
                )
            )
        return hits[:k]
