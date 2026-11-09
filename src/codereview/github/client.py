"""GitHub REST client with retries, pagination and rate-limit handling."""
from __future__ import annotations

import time

import httpx

from codereview.config import Settings
from codereview.errors import GitHubAPIError
from codereview.github.auth import GitHubAppAuth, TokenProvider, build_token_provider
from codereview.models import PRInfo
from codereview.retry import retry_after_seconds

_API_VERSION = "2022-11-28"


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">")
            if start != -1 and end != -1:
                return part[start + 1:end]
    return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])[:300]
    except ValueError:
        pass
    return (response.text or "")[:300]


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: TokenProvider | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.token_provider = token_provider or build_token_provider(settings)
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=settings.github_timeout,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- transport ---------------------------------------------------------

    def _token(self, installation_id: int | None) -> str | None:
        if self.token_provider is None:
            return None
        return self.token_provider.token_for(installation_id)

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str = "application/vnd.github+json",
        installation_id: int | None = None,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        full_url = url if url.startswith("http") else f"{self.settings.github_api_url}{url}"
        retries = max(1, self.settings.llm_retries)
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            token = self._token(installation_id)
            headers = {
                "Accept": accept,
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "codereview-agent/0.1",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            try:
                response = self._client.request(
                    method, full_url, headers=headers, params=params, json=json
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= retries:
                    raise GitHubAPIError(0, f"transport error: {exc}", full_url) from exc
                self._sleep(1.5 * attempt)
                continue

            # Expired installation token: drop cache and retry once.
            provider = self.token_provider
            if (
                response.status_code == 401
                and isinstance(provider, GitHubAppAuth)
                and installation_id
            ):
                provider.invalidate(installation_id)
                if attempt < retries:
                    continue

            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                if attempt < retries:
                    self._sleep(retry_after_seconds(response.headers.get("Retry-After")))
                    continue

            if response.status_code == 429:
                if attempt < retries:
                    self._sleep(retry_after_seconds(response.headers.get("Retry-After")))
                    continue

            if 500 <= response.status_code < 600:
                if attempt < retries:
                    self._sleep(1.5 * attempt)
                    continue

            if response.status_code >= 400:
                raise GitHubAPIError(response.status_code, _error_message(response), full_url)

            return response

        raise GitHubAPIError(0, f"request failed: {last_error}", full_url)

    def _paginate(
        self,
        url: str,
        *,
        installation_id: int | None = None,
        params: dict | None = None,
    ) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        next_params = params
        while next_url:
            response = self._request(
                "GET", next_url, installation_id=installation_id, params=next_params
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubAPIError(502, "expected a JSON array", next_url)
            items.extend(payload)
            next_url = _next_link(response.headers.get("Link"))
            next_params = None
        return items

    # --- API ---------------------------------------------------------------

    def get_pull(self, repo: str, number: int, installation_id: int | None = None) -> PRInfo:
        response = self._request(
            "GET", f"/repos/{repo}/pulls/{number}", installation_id=installation_id
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(502, "pull request payload is not an object")
        return PRInfo.from_api(repo, payload)

    def get_pull_diff(self, repo: str, number: int, installation_id: int | None = None) -> str:
        response = self._request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff",
            installation_id=installation_id,
        )
        return response.text

    def get_compare_diff(
        self, repo: str, base: str, head: str, installation_id: int | None = None
    ) -> str:
        """Diff for a push event: compare two commits with the diff media type."""
        response = self._request(
            "GET",
            f"/repos/{repo}/compare/{base}...{head}",
            accept="application/vnd.github.v3.diff",
            installation_id=installation_id,
        )
        return response.text

    def list_pull_files(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> list[dict]:
        return self._paginate(
            f"/repos/{repo}/pulls/{number}/files",
            installation_id=installation_id,
            params={"per_page": 100},
        )

    def get_repo_tree(
        self, repo: str, ref: str, installation_id: int | None = None
    ) -> list[dict]:
        response = self._request(
            "GET",
            f"/repos/{repo}/git/trees/{ref}",
            installation_id=installation_id,
            params={"recursive": "1"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(502, "tree payload is not an object")
        return list(payload.get("tree") or [])

    def download_tarball(
        self, repo: str, ref: str, installation_id: int | None = None
    ) -> bytes:
        """Stream a tarball into memory with a hard size ceiling.

        Streaming matters: a 200 MB repository must be rejected before the
        whole body is buffered, otherwise one webhook can exhaust the worker.
        """
        url = f"/repos/{repo}/tarball/{ref}"
        full_url = f"{self.settings.github_api_url}{url}"
        limit = self.settings.repo_max_mb * 1024 * 1024
        retries = max(1, self.settings.llm_retries)
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            token = self._token(installation_id)
            # GitHub serves the tarball via a redirect to codeload; the API
            # endpoint itself only accepts the JSON media type.
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "codereview-agent/0.1",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            try:
                with self._client.stream("GET", full_url, headers=headers) as response:
                    provider = self.token_provider
                    if (
                        response.status_code == 401
                        and isinstance(provider, GitHubAppAuth)
                        and installation_id
                    ):
                        provider.invalidate(installation_id)
                        if attempt < retries:
                            continue

                    if 500 <= response.status_code < 600 and attempt < retries:
                        response.read()
                        self._sleep(1.5 * attempt)
                        continue
                    if response.status_code == 429 and attempt < retries:
                        response.read()
                        self._sleep(retry_after_seconds(response.headers.get("Retry-After")))
                        continue
                    if response.status_code >= 400:
                        response.read()
                        raise GitHubAPIError(
                            response.status_code, _error_message(response), full_url
                        )

                    declared = response.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise GitHubAPIError(
                            413, f"tarball exceeds REPO_MAX_MB={self.settings.repo_max_mb}"
                        )

                    buffer = bytearray()
                    for chunk in response.iter_bytes(64 * 1024):
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise GitHubAPIError(
                                413,
                                f"tarball exceeds REPO_MAX_MB={self.settings.repo_max_mb}",
                            )
                    return bytes(buffer)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= retries:
                    raise GitHubAPIError(0, f"transport error: {exc}", full_url) from exc
                self._sleep(1.5 * attempt)

        raise GitHubAPIError(0, f"download failed: {last_error}", full_url)

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
        response = self._request(
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            installation_id=installation_id,
            json={"commit_id": commit_id, "body": body, "event": event, "comments": comments},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(502, "review payload is not an object")
        return payload

    def list_reviews(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> list[dict]:
        return self._paginate(
            f"/repos/{repo}/pulls/{number}/reviews",
            installation_id=installation_id,
            params={"per_page": 100},
        )

    def list_review_comments(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> list[dict]:
        return self._paginate(
            f"/repos/{repo}/pulls/{number}/comments",
            installation_id=installation_id,
            params={"per_page": 100},
        )

    def search_issues(
        self, query: str, *, per_page: int = 30, installation_id: int | None = None
    ) -> list[dict]:
        response = self._request(
            "GET",
            "/search/issues",
            installation_id=installation_id,
            params={"q": query, "per_page": per_page},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(502, "search payload is not an object")
        return list(payload.get("items") or [])

    def get_authenticated_user(self, installation_id: int | None = None) -> dict:
        response = self._request("GET", "/user", installation_id=installation_id)
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(502, "user payload is not an object")
        return payload
