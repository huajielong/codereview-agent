"""GitHub App authentication (RS256 JWT -> installation access token).

The private key is read from a path at first use and never logged. Tokens are
cached in memory until shortly before expiry.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx
import jwt

from codereview.config import Settings
from codereview.errors import GitHubAuthError


class TokenProvider(Protocol):
    def token_for(self, installation_id: int | None = None) -> str | None:
        ...


class StaticTokenProvider:
    """PAT fallback for single-repo setups."""

    def __init__(self, token: str):
        self._token = token

    def token_for(self, installation_id: int | None = None) -> str | None:
        return self._token


class GitHubAppAuth:
    def __init__(
        self,
        app_id: str,
        private_key_path: str,
        api_url: str = "https://api.github.com",
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        now=time.time,
        sleep=time.sleep,
    ):
        self.app_id = str(app_id)
        self.private_key_path = private_key_path
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._private_key: str | None = None
        self._tokens: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _load_private_key(self) -> str:
        if self._private_key is None:
            path = Path(self.private_key_path)
            try:
                self._private_key = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise GitHubAuthError(
                    f"cannot read GitHub App private key at {self.private_key_path}: {exc}"
                ) from exc
        return self._private_key

    def make_jwt(self) -> str:
        now = int(self._now())
        payload = {"iat": now - 60, "exp": now + 600, "iss": self.app_id}
        try:
            return jwt.encode(payload, self._load_private_key(), algorithm="RS256")
        except Exception as exc:  # noqa: BLE001 - normalize crypto errors
            raise GitHubAuthError(f"failed to sign GitHub App JWT: {exc}") from exc

    def installation_token(self, installation_id: int) -> str:
        if not installation_id:
            raise GitHubAuthError("installation_id is required for GitHub App auth")

        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached[1] - self._now() > 300:
                return cached[0]

            token = self._fetch_token(installation_id)
            self._tokens[installation_id] = token
            return token[0]

    def _fetch_token(self, installation_id: int) -> tuple[str, float]:
        url = f"{self.api_url}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.make_jwt()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codereview-agent/0.1",
        }
        try:
            with httpx.Client(
                timeout=self.timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"installation token request failed: {exc}") from exc

        if response.status_code not in (200, 201):
            raise GitHubAuthError(
                f"installation token request failed: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAuthError("installation token response is not JSON") from exc

        token = payload.get("token")
        if not token:
            raise GitHubAuthError("installation token response has no token")

        expires_at = payload.get("expires_at")
        expiry = self._now() + 3000.0
        if expires_at:
            try:
                parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                expiry = parsed.astimezone(timezone.utc).timestamp()
            except ValueError:
                pass
        return str(token), expiry

    def invalidate(self, installation_id: int) -> None:
        with self._lock:
            self._tokens.pop(installation_id, None)

    def token_for(self, installation_id: int | None = None) -> str | None:
        if installation_id is None:
            raise GitHubAuthError("installation_id is required for GitHub App auth")
        return self.installation_token(installation_id)


def build_token_provider(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> TokenProvider | None:
    if settings.has_github_app():
        return GitHubAppAuth(
            settings.github_app_id or "",
            settings.github_private_key_path or "",
            settings.github_api_url,
            timeout=settings.github_timeout,
            transport=transport,
        )
    if settings.github_token:
        return StaticTokenProvider(settings.github_token)
    return None
