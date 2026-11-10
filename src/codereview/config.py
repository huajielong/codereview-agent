"""Configuration loaded once per process from environment variables.

Deliberately no import-time validation: importing the package must never fail
because a developer machine has no API key. Call ``require_*`` at entry points.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Mapping

from codereview.errors import ConfigError

VALID_MODES = ("general", "security", "pattern", "ensemble")
VALID_SEVERITIES = ("low", "medium", "high")

# Bump this when review behaviour changes in a way that must invalidate
# persisted review caches (it participates in Settings.fingerprint()).
ENGINE_VERSION = "2026-11-09.1"


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], key: str, default: float, *, minimum: float = 0.0) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _text(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    raw = env.get(key)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _repos(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("REVIEW_ALLOWED_REPOS") or ""
    return tuple(sorted({item.strip().lower() for item in raw.split(",") if item.strip()}))


def _paths(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("REVIEW_IGNORE_PATHS") or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # LLM
    openai_api_key: str | None
    openai_base_url: str | None
    model: str | None
    max_tokens: int
    llm_timeout: float
    llm_retries: int

    # Embeddings
    cohere_api_key: str | None
    cohere_embed_model: str | None

    # GitHub
    github_token: str | None
    github_app_id: str | None
    github_private_key_path: str | None
    github_webhook_secret: str | None
    github_api_url: str
    github_timeout: float
    github_installation_id: int | None

    # Review
    mode: str
    dry_run: bool
    allowed_repos: tuple[str, ...]
    review_drafts: bool
    min_severity: str
    max_findings: int
    max_inline_comments: int
    max_diff_chars: int
    context_enabled: bool
    context_top_k: int
    policy_enabled: bool
    policy_path: str
    prompt_dir: str
    audit_path: str
    history_enabled: bool
    history_path: str
    dependency_enabled: bool
    post_empty_summary: bool
    post_on_partial: bool
    skip_bots: bool
    cache_enabled: bool

    # Storage
    db_path: str
    chroma_path: str
    cache_dir: str
    repo_cache_keep: int
    repo_max_mb: int

    # Server
    host: str
    port: int
    concurrency: int
    log_level: str
    metrics_enabled: bool
    ignore_paths: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        mode = (_text(e, "REVIEW_MODE", "security") or "security").lower()
        if mode not in VALID_MODES:
            raise ConfigError(f"REVIEW_MODE must be one of {VALID_MODES}, got {mode!r}")

        min_severity = (_text(e, "REVIEW_MIN_SEVERITY", "medium") or "medium").lower()
        if min_severity not in VALID_SEVERITIES:
            raise ConfigError(
                f"REVIEW_MIN_SEVERITY must be one of {VALID_SEVERITIES}, got {min_severity!r}"
            )

        installation_raw = _text(e, "GITHUB_INSTALLATION_ID")
        try:
            installation_id = int(installation_raw) if installation_raw else None
        except ValueError as exc:
            raise ConfigError("GITHUB_INSTALLATION_ID must be an integer") from exc

        return cls(
            openai_api_key=_text(e, "OPENAI_API_KEY"),
            openai_base_url=_text(e, "OPENAI_BASE_URL"),
            model=_text(e, "OPENAI_MODEL"),
            max_tokens=_int(e, "OPENAI_MAX_TOKENS", 16384, minimum=256),
            llm_timeout=_float(e, "OPENAI_TIMEOUT", 120.0, minimum=1.0),
            llm_retries=_int(e, "OPENAI_MAX_RETRIES", 3, minimum=1),
            cohere_api_key=_text(e, "COHERE_API_KEY"),
            cohere_embed_model=_text(e, "COHERE_EMBED_MODEL"),
            github_token=_text(e, "GITHUB_TOKEN"),
            github_app_id=_text(e, "GITHUB_APP_ID"),
            github_private_key_path=_text(e, "GITHUB_PRIVATE_KEY_PATH"),
            github_webhook_secret=_text(e, "GITHUB_WEBHOOK_SECRET"),
            github_api_url=(_text(e, "GITHUB_API_URL", "https://api.github.com") or "").rstrip("/"),
            github_timeout=_float(e, "GITHUB_TIMEOUT", 30.0, minimum=1.0),
            github_installation_id=installation_id,
            mode=mode,  # type: ignore[arg-type]
            dry_run=_bool(e, "REVIEW_DRY_RUN", True),
            allowed_repos=_repos(e),
            review_drafts=_bool(e, "REVIEW_DRAFTS", False),
            min_severity=min_severity,
            max_findings=_int(e, "REVIEW_MAX_FINDINGS", 10, minimum=1),
            max_inline_comments=_int(e, "REVIEW_MAX_INLINE_COMMENTS", 20, minimum=0),
            max_diff_chars=_int(e, "REVIEW_MAX_DIFF_CHARS", 12000, minimum=1000),
            context_enabled=_bool(e, "REVIEW_CONTEXT_ENABLED", True),
            context_top_k=_int(e, "REVIEW_CONTEXT_TOP_K", 12, minimum=1),
            policy_enabled=_bool(e, "REVIEW_POLICY_ENABLED", True),
            policy_path=_text(e, "REVIEW_POLICY_PATH", "policies/security.json") or "",
            prompt_dir=_text(e, "REVIEW_PROMPT_DIR", "prompts") or "",
            audit_path=_text(e, "REVIEW_AUDIT_PATH", "data/audit.jsonl") or "",
            history_enabled=_bool(e, "REVIEW_HISTORY_ENABLED", False),
            history_path=_text(e, "REVIEW_HISTORY_PATH", "data/history.jsonl") or "",
            dependency_enabled=_bool(e, "REVIEW_DEPENDENCY_ENABLED", False),
            post_empty_summary=_bool(e, "REVIEW_POST_EMPTY_SUMMARY", False),
            post_on_partial=_bool(e, "REVIEW_POST_ON_PARTIAL", False),
            skip_bots=_bool(e, "REVIEW_SKIP_BOTS", True),
            cache_enabled=_bool(e, "REVIEW_CACHE_ENABLED", True),
            db_path=_text(e, "DATABASE_PATH", "data/codereview.db") or "",
            chroma_path=_text(e, "CHROMA_PATH", "data/chroma") or "",
            cache_dir=_text(e, "CACHE_DIR", "data/cache") or "",
            repo_cache_keep=_int(e, "REPO_CACHE_KEEP", 3, minimum=1),
            repo_max_mb=_int(e, "REPO_MAX_MB", 200, minimum=1),
            host=_text(e, "SERVER_HOST", "0.0.0.0") or "0.0.0.0",
            port=_int(e, "SERVER_PORT", 8000, minimum=1),
            concurrency=_int(e, "REVIEW_CONCURRENCY", 2, minimum=1),
            log_level=(_text(e, "LOG_LEVEL", "INFO") or "INFO").upper(),
            metrics_enabled=_bool(e, "METRICS_ENABLED", True),
            ignore_paths=_paths(e),
        )

    # --- capability checks -------------------------------------------------

    def has_github_app(self) -> bool:
        return bool(self.github_app_id and self.github_private_key_path)

    def has_github_write(self) -> bool:
        return self.has_github_app() or bool(self.github_token)

    def llm_available(self) -> bool:
        return bool(self.openai_api_key and self.model)

    def embedding_available(self) -> bool:
        if self.cohere_api_key and self.cohere_embed_model:
            return True
        return bool(self.openai_api_key)

    def require_llm(self) -> None:
        if not self.model:
            raise ConfigError(
                "OPENAI_MODEL is not set. Put it in .env before starting the service."
            )
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY is not set.")

    def require_webhook(self) -> None:
        if not self.github_webhook_secret:
            raise ConfigError("GITHUB_WEBHOOK_SECRET is required to verify webhook signatures.")
        if not self.has_github_write():
            raise ConfigError(
                "Configure GITHUB_APP_ID + GITHUB_PRIVATE_KEY_PATH, or GITHUB_TOKEN."
            )

    def repo_allowed(self, repo: str) -> bool:
        return not self.allowed_repos or repo.lower() in self.allowed_repos

    # --- provenance --------------------------------------------------------

    def fingerprint(self) -> str:
        """Stable hash of behaviour-affecting, non-secret settings."""
        payload = {
            "engine_version": ENGINE_VERSION,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "mode": self.mode,
            "min_severity": self.min_severity,
            "max_findings": self.max_findings,
            "max_inline_comments": self.max_inline_comments,
            "post_empty_summary": self.post_empty_summary,
            "post_on_partial": self.post_on_partial,
            "max_diff_chars": self.max_diff_chars,
            "context_enabled": self.context_enabled,
            "context_top_k": self.context_top_k,
            "policy_enabled": self.policy_enabled,
            "history_enabled": self.history_enabled,
            "dependency_enabled": self.dependency_enabled,
            "ignore_paths": self.ignore_paths,
            "cohere_embed_model": self.cohere_embed_model,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def redacted(self) -> dict:
        data = asdict(self)
        for key in (
            "openai_api_key",
            "cohere_api_key",
            "github_token",
            "github_webhook_secret",
            "github_private_key_path",
        ):
            if data.get(key):
                data[key] = "***"
        data["allowed_repos"] = list(self.allowed_repos)
        return data
