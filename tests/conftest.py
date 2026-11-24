"""Offline fixtures for the review pipeline.

Nothing in this directory touches GitHub, GitLab, an LLM endpoint or the
network. Fakes implement exactly the duck-typed surface the production code
uses, so a change to that contract breaks the tests instead of production.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codereview.llm.client import LLMResponse
from codereview.models import PRInfo

SAMPLE_DIFF = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 import sqlite3
 x = 1
+user_id = input()
+query = "SELECT * FROM users WHERE id = " + user_id
"""

SAMPLE_FINDING_TEXT = """\
ISSUE: SQL injection through string concatenation
SEVERITY: high
FILE: app.py
LINE: 3
EVIDENCE: query = "SELECT * FROM users WHERE id = " + user_id
"""


@pytest.fixture
def settings(tmp_path: Path):
    from codereview.config import Settings

    return Settings.from_env(
        {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "test-model",
            "REVIEW_CONTEXT_ENABLED": "false",
            "REVIEW_POLICY_ENABLED": "false",
            "REVIEW_HISTORY_ENABLED": "false",
            "REVIEW_DEPENDENCY_ENABLED": "false",
            "REVIEW_DRY_RUN": "true",
            "REVIEW_POST_EMPTY_SUMMARY": "true",
            "DATABASE_PATH": str(tmp_path / "codereview.db"),
            "CHROMA_PATH": str(tmp_path / "chroma"),
            "CACHE_DIR": str(tmp_path / "cache"),
            "REVIEW_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
            "REVIEW_HISTORY_PATH": str(tmp_path / "history.jsonl"),
            "REVIEW_PROMPT_DIR": str(tmp_path / "prompts"),
            "REVIEW_POLICY_PATH": str(tmp_path / "policy.json"),
            "REVIEW_IGNORE_PATHS": "",
        }
    )


class FakeLLM:
    """Minimal LLMClient.complete replacement."""

    def __init__(self, content: str = SAMPLE_FINDING_TEXT, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []

    def complete(self, system: str, user: str, **kwargs) -> LLMResponse:
        self.calls.append((system, user, kwargs))
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=self.content,
            prompt_tokens=10,
            completion_tokens=10,
            model="fake",
        )


class FakeGitHub:
    """Duck-typed stand-in for GitHubClient.

    ``create_error`` is raised for the first ``error_times`` calls that carry
    inline comments; later calls succeed. This exercises the 422 fallback.
    """

    def __init__(
        self,
        *,
        pr: PRInfo | None = None,
        diff: str = SAMPLE_DIFF,
        reviews: list[dict] | None = None,
        create_error: Exception | None = None,
        error_times: int = 1,
        compare_diff: str = "",
    ):
        self.pr = pr or PRInfo(
            repo="owner/repo",
            number=7,
            title="Add user lookup",
            body="",
            author="alice",
            base_sha="base-sha",
            head_sha="head-sha",
            state="open",
            draft=False,
            merged=False,
            html_url="https://github.com/owner/repo/pull/7",
        )
        self.diff = diff
        self.reviews = list(reviews or [])
        self.create_error = create_error
        self.error_times = error_times
        self.compare_diff = compare_diff
        self.created: list[dict] = []
        self.compare_calls: list[tuple] = []
        self.closed = False

    def get_pull(self, repo: str, number: int, installation_id=None) -> PRInfo:
        return self.pr

    def get_pull_diff(self, repo: str, number: int, installation_id=None) -> str:
        return self.diff

    def get_compare_diff(self, repo: str, base: str, head: str, installation_id=None) -> str:
        self.compare_calls.append((repo, base, head, installation_id))
        return self.compare_diff

    def list_reviews(self, repo: str, number: int, installation_id=None) -> list[dict]:
        return self.reviews

    def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict],
        event: str = "COMMENT",
        installation_id=None,
    ) -> dict:
        if self.create_error is not None and comments and self.error_times > 0:
            self.error_times -= 1
            raise self.create_error
        self.created.append(
            {
                "repo": repo,
                "number": number,
                "commit_id": commit_id,
                "body": body,
                "comments": comments,
                "event": event,
            }
        )
        return {"id": 100 + len(self.created)}

    def download_tarball(self, repo: str, ref: str, installation_id=None) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


class FakeGitLab(FakeGitHub):
    """Duck-typed stand-in for GitLabClient (same surface as FakeGitHub)."""

    provider_name = "gitlab"

    def __init__(self, **kwargs):
        pr = kwargs.pop(
            "pr",
            PRInfo(
                repo="gitlab.com/group/proj",
                number=3,
                title="Add user lookup",
                body="",
                author="alice",
                base_sha="gl-base",
                head_sha="gl-head",
                state="open",
                draft=False,
                merged=False,
                html_url="https://gitlab.com/group/proj/-/merge_requests/3",
            ),
        )
        super().__init__(pr=pr, **kwargs)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def fake_gitlab() -> FakeGitLab:
    return FakeGitLab()
