"""Embedding providers plus a content-addressed SQLite cache.

The cache is what makes incremental indexing cheap: unchanged chunks never hit
the embedding API again, even across process restarts or different PRs.
"""
from __future__ import annotations

import hashlib
from typing import Protocol

import httpx

from codereview.config import Settings
from codereview.errors import EmbeddingError
from codereview.retry import call_with_retries
from codereview.storage.db import Database

_MAX_TEXT_CHARS = 12_000


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        ...


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if type(exc).__name__ in (
        "APIConnectionError", "APITimeoutError", "InternalServerError",
        "RateLimitError", "ServiceUnavailableError", "RemoteProtocolError",
    ):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


class CohereProvider:
    def __init__(self, api_key: str, model: str, timeout: float = 60.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = f"cohere:{model}"
        self._client = None

    def _get_client(self):
        if self._client is None:
            import cohere

            self._client = cohere.Client(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def _embed_batch(self, texts: list[str], input_type: str) -> list[list[float]]:
        response = self._get_client().embed(
            texts=texts,
            model=self.model,
            input_type=input_type,
            truncate="END",
        )
        embeddings = response.embeddings
        return [list(item) for item in getattr(embeddings, "float_", embeddings)]

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 96):
            batch = texts[start:start + 96]
            try:
                vectors.extend(
                    call_with_retries(
                        lambda b=batch: self._embed_batch(b, input_type),
                        attempts=3,
                        should_retry=_is_transient,
                        on_retry=lambda *_: setattr(self, "_client", None),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"Cohere embedding failed: {exc}") from exc
        return vectors


class OpenAIEmbeddingProvider:
    def __init__(self, api_key: str, base_url: str | None, model: str, timeout: float = 60.0):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.name = f"openai:{model}"
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        return self._client

    def _embed_batch(self, texts: list[str], input_type: str) -> list[list[float]]:
        response = self._get_client().embeddings.create(model=self.model, input=texts)
        return [list(item.embedding) for item in response.data]

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 64):
            batch = texts[start:start + 64]
            try:
                vectors.extend(
                    call_with_retries(
                        lambda b=batch: self._embed_batch(b, input_type),
                        attempts=3,
                        should_retry=_is_transient,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"OpenAI-compatible embedding failed: {exc}") from exc
        return vectors


class EmbeddingService:
    def __init__(self, db: Database, provider: EmbeddingProvider):
        self.db = db
        self.provider = provider
        self.model_name = provider.name
        self.last_hits = 0
        self.last_misses = 0

    def _key(self, text: str, input_type: str) -> str:
        blob = f"{self.model_name}\0{input_type}\0{text}"
        return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()

    def _prepare(self, texts: list[str]) -> list[str]:
        return [str(text)[:_MAX_TEXT_CHARS] for text in texts]

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        prepared = self._prepare(texts)
        keys = [self._key(text, input_type) for text in prepared]
        cached = self.db.embedding_get_hashes(self.model_name, keys)

        missing: dict[str, int] = {}
        for index, key in enumerate(keys):
            if key not in cached:
                missing.setdefault(key, index)

        self.last_hits = len(prepared) - len(missing)
        self.last_misses = len(missing)

        if missing:
            missing_keys = list(missing)
            missing_texts = [prepared[missing[key]] for key in missing_keys]
            vectors = self.provider.embed(missing_texts, input_type)
            if len(vectors) != len(missing_texts):
                raise EmbeddingError(
                    f"embedding provider returned {len(vectors)} vectors for {len(missing_texts)} texts"
                )
            dims = {len(vec) for vec in vectors}
            if len(dims) > 1:
                raise EmbeddingError(f"inconsistent embedding dimensions: {dims}")
            entries = list(zip(missing_keys, [self.model_name] * len(missing_keys), vectors))
            self.db.embedding_put_many(entries)
            for key, vector in zip(missing_keys, vectors):
                cached[key] = vector

        return [cached[key] for key in keys]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "search_document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "search_query")[0]


def build_embedding_service(settings: Settings, db: Database) -> EmbeddingService | None:
    if settings.cohere_api_key and settings.cohere_embed_model:
        return EmbeddingService(
            db, CohereProvider(settings.cohere_api_key, settings.cohere_embed_model)
        )
    if settings.openai_api_key and settings.model:
        return EmbeddingService(
            db,
            OpenAIEmbeddingProvider(
                settings.openai_api_key, settings.openai_base_url, settings.model
            ),
        )
    return None
