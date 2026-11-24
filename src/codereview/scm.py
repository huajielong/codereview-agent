"""Provider-neutral source-control helpers.

The review pipeline only needs a small duck-typed surface (get a change, get
its diff, list existing reviews, publish, download an archive, compare two
refs). ``CodeHost`` documents that surface; both ``GitHubClient`` and
``GitLabClient`` implement it without inheriting from a base class.

Repository identity is intentionally asymmetric:

* GitHub keeps the historical ``owner/repo`` key, so existing databases keep
  their cache and history.
* GitLab uses ``<gitlab host>/<namespace path>`` (for example
  ``gitlab.com/group/proj``), which keeps the shared SQLite tables free of
  collisions and needs no schema migration.
"""
from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse

from codereview.errors import ConfigError
from codereview.models import PRInfo

PROVIDERS = ("github", "gitlab")


class CodeHost(Protocol):
    """The subset of a code-host client used by pipeline/publisher/workspace."""

    def get_pull(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> PRInfo: ...

    def get_pull_diff(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> str: ...

    def get_compare_diff(
        self, repo: str, base: str, head: str, installation_id: int | None = None
    ) -> str: ...

    def list_reviews(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> list[dict]: ...

    def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict],
        event: str = "COMMENT",
        installation_id: int | None = None,
    ) -> dict: ...

    def download_tarball(
        self, repo: str, ref: str, installation_id: int | None = None
    ) -> bytes: ...

    def close(self) -> None: ...


def _strip_dot_git(path: str) -> str:
    path = path.strip().strip("/")
    return path[:-4] if path.endswith(".git") else path


def _split_url(repo: str) -> tuple[str, str] | None:
    """Return ``(netloc, path)`` for an http(s)/git@ repository reference."""
    text = repo.strip()
    if text.startswith("git@"):
        host, _, path = text[4:].partition(":")
        return host.lower(), path
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return parsed.netloc.lower(), parsed.path
    return None


def qualify_repo(provider: str, base_url: str, repo: str) -> str:
    """Normalize a CLI/webhook repository reference to its internal key.

    GitHub: ``owner/repo`` (unchanged from the pre-GitLab behaviour).
    GitLab: ``<host>/<group>/<project>``.
    """
    if provider not in PROVIDERS:
        raise ConfigError(f"unknown SCM provider: {provider!r}")
    text = (repo or "").strip()
    if not text:
        raise ConfigError("repository is empty")

    if provider == "github":
        split = _split_url(text)
        if split:
            host, path = split
            if host not in ("github.com", "www.github.com"):
                raise ConfigError(f"not a github.com repository: {repo!r}")
            text = path
        elif text.lower().startswith("github.com/"):
            text = text[len("github.com/"):]
        path = _strip_dot_git(text)
        if not path or "/" not in path:
            raise ConfigError(f"GitHub repository must be owner/repo, got {repo!r}")
        return path

    host = urlparse(base_url).netloc.lower()
    if not host:
        raise ConfigError(f"GITLAB_BASE_URL has no host: {base_url!r}")
    split = _split_url(text)
    if split:
        ref_host, path = split
        if ref_host != host:
            raise ConfigError(
                f"repository host {ref_host!r} does not match GITLAB_BASE_URL {host!r}"
            )
        text = path
    elif text.lower().startswith(host + "/"):
        text = text[len(host) + 1:]
    path = _strip_dot_git(text)
    if not path or "/" not in path:
        raise ConfigError(
            f"GitLab repository must be group/project (or host/group/project), got {repo!r}"
        )
    return f"{host}/{path}"


def project_path(repo: str, base_url: str) -> str:
    """Strip the GitLab host from an internal repo key, returning ``group/proj``."""
    host = urlparse(base_url).netloc.lower()
    text = (repo or "").strip().strip("/")
    if host and text.lower().startswith(host + "/"):
        text = text[len(host) + 1:]
    return _strip_dot_git(text)


def provider_for_repo(repo: str, settings) -> str:
    """Classify an internal repo key. Bare keys stay GitHub for compatibility."""
    host = urlparse(settings.gitlab_base_url).netloc.lower()
    text = (repo or "").lower()
    if host and text.startswith(host + "/"):
        return "gitlab"
    return "github"


class ProviderRouter:
    """Route CodeHost calls to GitHub/GitLab by the repo's host prefix."""

    provider_name = "router"

    def __init__(self, settings, hosts: dict[str, CodeHost]):
        if not hosts:
            raise ConfigError("ProviderRouter needs at least one host")
        self.settings = settings
        self.hosts = dict(hosts)

    def host_for(self, repo: str) -> CodeHost:
        provider = provider_for_repo(repo, self.settings)
        host = self.hosts.get(provider)
        if host is not None:
            return host
        if len(self.hosts) == 1:
            return next(iter(self.hosts.values()))
        raise ConfigError(
            f"no {provider} host is configured for repository {repo!r}; "
            f"configured providers: {sorted(self.hosts)}"
        )

    def get_pull(self, repo: str, number: int, installation_id: int | None = None) -> PRInfo:
        return self.host_for(repo).get_pull(repo, number, installation_id)

    def get_pull_diff(self, repo: str, number: int, installation_id: int | None = None) -> str:
        return self.host_for(repo).get_pull_diff(repo, number, installation_id)

    def get_compare_diff(
        self, repo: str, base: str, head: str, installation_id: int | None = None
    ) -> str:
        return self.host_for(repo).get_compare_diff(repo, base, head, installation_id)

    def list_reviews(self, repo: str, number: int, installation_id: int | None = None) -> list[dict]:
        return self.host_for(repo).list_reviews(repo, number, installation_id)

    def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict],
        event: str = "COMMENT",
        installation_id: int | None = None,
    ) -> dict:
        return self.host_for(repo).create_review(
            repo,
            number,
            commit_id=commit_id,
            body=body,
            comments=comments,
            event=event,
            installation_id=installation_id,
        )

    def download_tarball(self, repo: str, ref: str, installation_id: int | None = None) -> bytes:
        return self.host_for(repo).download_tarball(repo, ref, installation_id)

    def close(self) -> None:
        for host in self.hosts.values():
            try:
                host.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass


def coerce_provider(value: str | None, default: str = "github") -> str:
    provider = (value or default).strip().lower()
    if provider not in PROVIDERS:
        raise ConfigError(f"provider must be one of {PROVIDERS}, got {provider!r}")
    return provider


__all__ = [
    "CodeHost",
    "PROVIDERS",
    "ProviderRouter",
    "coerce_provider",
    "project_path",
    "provider_for_repo",
    "qualify_repo",
]
