"""GitLab publishing: summary-last, per-comment markers, partial retry."""
from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from codereview.config import Settings
from codereview.errors import GitLabAPIError
from codereview.gitlab.client import GitLabClient
from codereview.models import Finding
from codereview.review.publisher import Publisher, fingerprint_findings
from codereview.storage.db import Database

from tests.conftest import FakeGitLab

REPO = "gitlab.com/group/proj"
NUMBER = 3
HEAD = "gl-head"
FINDINGS = [Finding(issue="SQL injection", severity="high", file="app.py", line=2)]
VALID_LINES = {"app.py": {2}}
FP = "abcdef0123456789"
SUMMARY = f"## AI Code Review\n\n<!-- codereview:{FP} -->"


def gitlab_settings(**overrides) -> Settings:
    env = {
        "OPENAI_API_KEY": "test-key",
        "OPENAI_MODEL": "test-model",
        "GITLAB_TOKEN": "glpat-test",
        "GITLAB_BASE_URL": "https://gitlab.com",
        "REVIEW_CONTEXT_ENABLED": "false",
        "REVIEW_POST_EMPTY_SUMMARY": "true",
    }
    env.update(overrides)
    return Settings.from_env(env)


def mr_payload(**overrides) -> dict:
    payload = {
        "iid": NUMBER,
        "state": "opened",
        "merged_at": None,
        "sha": HEAD,
        "diff_refs": {
            "base_sha": "base",
            "head_sha": HEAD,
            "start_sha": "start",
        },
        "changes_count": "1",
    }
    payload.update(overrides)
    return payload


def test_publisher_posts_through_gitlab_and_marks_db(settings, fake_gitlab):
    db = Database(settings.db_path)
    try:
        publisher = Publisher(settings, fake_gitlab, db)
        result = publisher.publish(
            repo=REPO,
            number=NUMBER,
            head_sha=HEAD,
            findings=FINDINGS,
            valid_lines=VALID_LINES,
            mode="security",
            config_hash="cfg",
            dry_run=False,
        )
    finally:
        db.close()

    assert result.posted is True
    assert len(fake_gitlab.created) == 1
    assert fake_gitlab.created[0]["comments"][0]["path"] == "app.py"
    assert "<!-- codereview:" in fake_gitlab.created[0]["body"]


def test_publisher_marker_skip_with_gitlab(settings, fake_gitlab):
    fingerprint = fingerprint_findings(REPO, NUMBER, HEAD, "security", FINDINGS, "cfg")
    fake_gitlab.reviews = [{"id": 9, "body": f"old <!-- codereview:{fingerprint} -->"}]
    db = Database(settings.db_path)
    try:
        result = Publisher(settings, fake_gitlab, db).publish(
            repo=REPO,
            number=NUMBER,
            head_sha=HEAD,
            findings=FINDINGS,
            valid_lines=VALID_LINES,
            mode="security",
            config_hash="cfg",
            dry_run=False,
        )
    finally:
        db.close()

    assert result.posted is False
    assert "already posted" in (result.skipped_reason or "")
    assert fake_gitlab.created == []


def test_publisher_does_not_retry_gitlab_failure_as_summary(settings):
    fake = FakeGitLab(create_error=GitLabAPIError(403, "no notes permission"))
    db = Database(settings.db_path)
    try:
        result = Publisher(settings, fake, db).publish(
            repo=REPO,
            number=NUMBER,
            head_sha=HEAD,
            findings=FINDINGS,
            valid_lines=VALID_LINES,
            mode="security",
            config_hash="cfg",
            dry_run=False,
        )
    finally:
        db.close()

    assert result.posted is False
    assert result.errors
    assert fake.created == []


class GitLabStub:
    """Stateful MockTransport handler for create_review."""

    def __init__(
        self,
        *,
        state="opened",
        head=HEAD,
        heads=None,
        notes_status=201,
        discussion_status=201,
    ):
        self.heads = list(heads) if heads else [head]
        self._mr_gets = 0
        self.mr = mr_payload(state=state, diff_refs={
            "base_sha": "base", "head_sha": self.heads[0], "start_sha": "start"
        })
        self.notes_status = notes_status
        self.discussion_status = discussion_status
        self.discussions: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.forms: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if method == "GET" and path.endswith(f"/merge_requests/{NUMBER}"):
            current_head = self.heads[min(self._mr_gets, len(self.heads) - 1)]
            self._mr_gets += 1
            self.mr["sha"] = current_head
            self.mr["diff_refs"] = {
                "base_sha": "base",
                "head_sha": current_head,
                "start_sha": "start",
            }
            return httpx.Response(200, json=self.mr)
        if method == "GET" and path.endswith("/discussions"):
            return httpx.Response(200, json=self.discussions)
        if method == "GET" and path.endswith("/diffs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "old_path": "app.py",
                        "new_path": "app.py",
                        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x\n+y\n",
                    }
                ],
            )
        if method == "POST" and path.endswith("/discussions"):
            form = parse_qs(request.content.decode("utf-8"))
            self.forms.append(form)
            if self.discussion_status >= 400:
                return httpx.Response(
                    self.discussion_status, json={"message": "Note position is invalid"}
                )
            return httpx.Response(201, json={"id": len(self.forms)})
        if method == "POST" and path.endswith("/notes"):
            self.forms.append(parse_qs(request.content.decode("utf-8")))
            if self.notes_status >= 400:
                return httpx.Response(self.notes_status, json={"message": "forbidden"})
            return httpx.Response(201, json={"id": 99})
        raise AssertionError(f"unexpected request {method} {path}")


def make_client(stub: GitLabStub) -> GitLabClient:
    return GitLabClient(
        gitlab_settings(),
        transport=httpx.MockTransport(stub),
        sleep=lambda _seconds: None,
        clock=lambda: 1_000,
    )


def test_create_review_posts_inline_before_summary():
    stub = GitLabStub()
    client = make_client(stub)
    result = client.create_review(
        "group/proj",
        NUMBER,
        commit_id=HEAD,
        body=SUMMARY,
        comments=[{"path": "app.py", "line": 2, "side": "RIGHT", "body": "finding"}],
    )

    assert result == {"id": 99, "inline_posted": 1, "inline_skipped": 0}
    posts = [call for call in stub.calls if call[0] == "POST"]
    assert posts[-2][1].endswith("/discussions")
    assert posts[-1][1].endswith("/notes")

    form = stub.forms[0]
    assert form["position[position_type]"] == ["text"]
    assert form["position[base_sha]"] == ["base"]
    assert form["position[head_sha]"] == [HEAD]
    assert form["position[start_sha]"] == ["start"]
    assert form["position[new_path]"] == ["app.py"]
    assert form["position[old_path]"] == ["app.py"]
    assert form["position[new_line]"] == ["2"]
    assert f"codereview-inline:{FP}:0" in form["body"][0]
    assert f"codereview:{FP}" in stub.forms[-1]["body"][0]


def test_create_review_skips_already_posted_inline_marker():
    stub = GitLabStub()
    stub.discussions = [
        {
            "notes": [
                {"body": f"old\n\n<!-- codereview-inline:{FP}:0 -->"},
            ]
        }
    ]
    client = make_client(stub)
    result = client.create_review(
        "group/proj",
        NUMBER,
        commit_id=HEAD,
        body=SUMMARY,
        comments=[
            {"path": "app.py", "line": 2, "side": "RIGHT", "body": "one"},
            {"path": "app.py", "line": 2, "side": "RIGHT", "body": "two"},
        ],
    )

    assert result["inline_posted"] == 1
    assert result["inline_skipped"] == 1
    discussion_posts = [
        call for call in stub.calls if call[0] == "POST" and call[1].endswith("/discussions")
    ]
    assert len(discussion_posts) == 1
    assert f"codereview-inline:{FP}:1" in stub.forms[0]["body"][0]


def test_create_review_invalid_position_does_not_fail_summary():
    stub = GitLabStub(discussion_status=400)
    client = make_client(stub)
    result = client.create_review(
        "group/proj",
        NUMBER,
        commit_id=HEAD,
        body=SUMMARY,
        comments=[{"path": "app.py", "line": 2, "side": "RIGHT", "body": "finding"}],
    )

    assert result["id"] == 99
    assert result["inline_posted"] == 0
    assert result["inline_skipped"] == 1
    assert any(call[1].endswith("/notes") for call in stub.calls)


@pytest.mark.parametrize("status", [401, 409])
def test_create_review_inline_non_position_error_fails_closed(status):
    stub = GitLabStub(discussion_status=status)
    client = make_client(stub)
    with pytest.raises(GitLabAPIError) as exc:
        client.create_review(
            "group/proj",
            NUMBER,
            commit_id=HEAD,
            body=SUMMARY,
            comments=[{"path": "app.py", "line": 2, "side": "RIGHT", "body": "finding"}],
        )

    assert exc.value.status == status
    assert not any(call[1].endswith("/notes") for call in stub.calls)


def test_create_review_rechecks_head_before_summary():
    stub = GitLabStub(heads=[HEAD, "new-head"])
    client = make_client(stub)
    with pytest.raises(GitLabAPIError) as exc:
        client.create_review(
            "group/proj",
            NUMBER,
            commit_id=HEAD,
            body=SUMMARY,
            comments=[{"path": "app.py", "line": 2, "side": "RIGHT", "body": "finding"}],
        )

    assert exc.value.status == 409
    posts = [call for call in stub.calls if call[0] == "POST"]
    assert any(call[1].endswith("/discussions") for call in posts)
    assert not any(call[1].endswith("/notes") for call in posts)


def test_create_review_stale_head_sends_nothing():
    stub = GitLabStub(head="new-head")
    client = make_client(stub)
    with pytest.raises(GitLabAPIError) as exc:
        client.create_review(
            "group/proj",
            NUMBER,
            commit_id="old-head",
            body=SUMMARY,
            comments=[],
        )
    assert exc.value.status == 409
    assert not [call for call in stub.calls if call[0] == "POST"]


def test_create_review_summary_failure_raises():
    stub = GitLabStub(notes_status=403)
    client = make_client(stub)
    with pytest.raises(GitLabAPIError) as exc:
        client.create_review(
            "group/proj",
            NUMBER,
            commit_id=HEAD,
            body=SUMMARY,
            comments=[],
        )
    assert exc.value.status == 403
