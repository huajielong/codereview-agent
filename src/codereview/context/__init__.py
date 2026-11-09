"""Repository context engine: chunking, cached embeddings, vector index."""

from codereview.context.chunker import Chunk, chunk_file, iter_repo_files
from codereview.context.embedding_cache import EmbeddingService, build_embedding_service
from codereview.context.index import RepoIndexer
from codereview.context.retriever import ContextRetriever
from codereview.context.store import ChromaVectorStore, MemoryVectorStore, VectorRecord

__all__ = [
    "Chunk",
    "chunk_file",
    "iter_repo_files",
    "EmbeddingService",
    "build_embedding_service",
    "RepoIndexer",
    "ContextRetriever",
    "ChromaVectorStore",
    "MemoryVectorStore",
    "VectorRecord",
]
