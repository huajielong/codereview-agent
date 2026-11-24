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
from codereview.gitlab.client import GitLabClient
from codereview.llm.client import LLMClient
from codereview.llm.prompts import PromptRegistry
from codereview.llm.reviewers import Reviewer
from codereview.logging_utils import get_logger
from codereview.review.history import HistoryStore
from codereview.review.pipeline import ReviewPipeline
from codereview.review.policy import load_policy
from codereview.review.publisher import Publisher
from codereview.review.workspace import RepoWorkspace
from codereview.scm import CodeHost, ProviderRouter, coerce_provider
from codereview.storage.db import Database

log = get_logger(__name__)


def _make_store(settings: Settings):
    try:
        return ChromaVectorStore(settings.chroma_path)
    except Exception as exc:  # noqa: BLE001 - fall back to in-process index
        log.warning("chroma_unavailable_fallback_memory", extra={"error": str(exc)})
        return MemoryVectorStore()


def build_code_host(settings: Settings, provider: str) -> CodeHost:
    provider = coerce_provider(provider)
    if provider == "github":
        return GitHubClient(settings, build_token_provider(settings))
    return GitLabClient(settings)


def _build_hosts(settings: Settings) -> CodeHost:
    """Auto-select one host, or a router when both providers are configured."""
    hosts: dict[str, CodeHost] = {}
    if settings.has_github_write() or not settings.has_gitlab_write():
        hosts["github"] = build_code_host(settings, "github")
    if settings.has_gitlab_write():
        hosts["gitlab"] = build_code_host(settings, "gitlab")
    if len(hosts) == 1:
        return next(iter(hosts.values()))
    return ProviderRouter(settings, hosts)


def build_pipeline(
    settings: Settings,
    *,
    db: Database | None = None,
    github: CodeHost | None = None,
    llm: LLMClient | None = None,
    provider: str | None = None,
) -> ReviewPipeline:
    settings.require_llm()
    database = db or Database(settings.db_path)

    if github is None:
        if provider in (None, "auto"):
            github = _build_hosts(settings)
        else:
            github = build_code_host(settings, provider)
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
