"""Compose the production pipeline from settings."""
from __future__ import annotations

from pathlib import Path

from codereview.config import Settings
from codereview.context.embedding_cache import build_embedding_service
from codereview.context.index import RepoIndexer
from codereview.context.retriever import ContextRetriever
from codereview.context.store import ChromaVectorStore, MemoryVectorStore
from codereview.github.auth import build_token_provider
from codereview.github.client import GitHubClient
from codereview.llm.client import LLMClient
from codereview.llm.prompts import PromptRegistry
from codereview.llm.reviewers import Reviewer
from codereview.logging_utils import get_logger
from codereview.review.history import HistoryStore
from codereview.review.pipeline import ReviewPipeline
from codereview.review.policy import load_policy
from codereview.review.publisher import Publisher
from codereview.review.workspace import RepoWorkspace
from codereview.storage.db import Database

log = get_logger(__name__)


def _make_store(settings: Settings):
    try:
        return ChromaVectorStore(settings.chroma_path)
    except Exception as exc:  # noqa: BLE001 - fall back to in-process index
        log.warning("chroma_unavailable_fallback_memory", extra={"error": str(exc)})
        return MemoryVectorStore()


def build_pipeline(
    settings: Settings,
    *,
    db: Database | None = None,
    github: GitHubClient | None = None,
    llm: LLMClient | None = None,
) -> ReviewPipeline:
    settings.require_llm()
    database = db or Database(settings.db_path)

    if github is None:
        provider = build_token_provider(settings)
        github = GitHubClient(settings, provider)
    if llm is None:
        llm = LLMClient(settings)

    registry = PromptRegistry()
    loaded_prompts = registry.load_directory(settings.prompt_dir)
    if loaded_prompts:
        log.info("prompt_overrides_loaded", extra={"count": loaded_prompts})
    reviewer = Reviewer(llm, registry)
    embeddings = build_embedding_service(settings, database) if settings.context_enabled else None
    store = _make_store(settings) if embeddings else MemoryVectorStore()
    indexer = RepoIndexer(database, embeddings, store)
    retriever = ContextRetriever(embeddings, store)
    workspace = RepoWorkspace(settings, github)

    policy = None
    if settings.policy_enabled:
        policy_path = Path(settings.policy_path)
        if policy_path.exists():
            policy = load_policy(policy_path)
        else:
            log.warning("policy_file_missing", extra={"path": str(policy_path)})

    history = HistoryStore(settings.history_path) if settings.history_enabled else None
    publisher = Publisher(settings, github, database)

    return ReviewPipeline(
        settings,
        database,
        github,
        reviewer,
        publisher,
        indexer=indexer,
        retriever=retriever,
        workspace=workspace,
        policy=policy,
        history=history,
    )
