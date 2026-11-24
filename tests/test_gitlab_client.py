"""GitLab REST adapter: read path, pagination, retries and diff integrity."""
from __future__ import annotations

import httpx
import pytest

from codereview.config import Settings
from codereview.diff.parser import parse_unified_diff, valid_line_map
from codereview.errors import GitLabAPIError, GitLabAuthError
from codereview.gitlab.client import GitLabClient


def gl_settings(**overrides) -> Settings:
    env = {
        "OPENAI_API_KEY": "test-key",
        "OPENAI_MODEL": "test-model",
        "GITLAB_TOKEN": "glpat-test",
        "GITLAB_BASE_URL": "https://gitlab.com",
        "REVIEW_CONTEXT_ENABLED": "false",
    }
    env.update(overrides)
    return Settings.from_env(env)


def make_client(handler, **overrides) -> tuple[GitLabClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = GitLabClient(
        gl_settings(**overrides),
        transport=httpx.MockTransport(wrapped),
        sleep=lambda _seconds: None,
        clock=lambda: 1_000,
    )
    return client, requests


def mr_payload(**overrides) -> dict:
    payload = {
        "iid": 7,
        "title": "Add lookup",
        "description": "body",
        "state": "opened",
        "draft": False,
        "work_in_progress": False,
        "sha": "head-sha",
        "merged_at": None,
        "diff_refs": {
            "base_sha": "base-sha",
            "head_sha": "head-sha",
            "start_sha": "start-sha",
        },
        "author": {"username": "alice"},
        "web_url": "https://gitlab.com/group/sub/proj/-/merge_requests/7",
        "changes_count": "3",
    }
    payload.update(overrides)
    return payload


def test_get_pull_maps_fields_and_encodes_project():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "group%2Fsub%2Fproj" in str(request.url)
        assert request.headers["PRIVATE-TOKEN"] == "glpat-test"
        return httpx.Response(200, json=mr_payload())

    client, requests = make_client(handler)
    pr = client.get_pull("group/sub/proj", 7)

    assert pr.repo == "gitlab.com/group/sub/proj"
    assert pr.number == 7
    assert pr.title == "Add lookup"
    assert pr.body == "body"
    assert pr.author == "alice"
    assert pr.head_sha == "head-sha"
    assert pr.base_sha == "base-sha"
    assert pr.state == "open"
    assert pr.draft is False
    assert pr.merged is False
    assert pr.changed_files == 3
    assert pr.html_url.endswith("/merge_requests/7")
    assert len(requests) == 1


def test_get_pull_head_falls_back_to_sha_and_merged_state():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=mr_payload(
                state="merged",
                merged_at="2026-01-01T00:00:00Z",
                diff_refs={},
                sha="fallback-sha",
            ),
        )

    client, _ = make_client(handler)
    pr = client.get_pull("group/proj", 7)
    assert pr.head_sha == "fallback-sha"
    assert pr.merged is True
    assert pr.state == "merged"


def test_get_pull_diff_reconstructs_multiple_file_types():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/diffs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "old_path": "app.py",
                        "new_path": "app.py",
                        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x\n+y\n",
                    },
                    {
                        "old_path": "new.py",
                        "new_path": "new.py",
                        "new_file": True,
                        "diff": "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,1 @@\n+z\n",
                    },
                    {
                        "old_path": "gone.py",
                        "new_path": "gone.py",
                        "deleted_file": True,
                        "diff": "--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n",
                    },
                ],
            )
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=mr_payload(changes_count="3"))
        raise AssertionError(f"unexpected request {request.url}")

    client, _ = make_client(handler)
    text = client.get_pull_diff("group/proj", 7)
    files = parse_unified_diff(text)

    assert [f.path for f in files] == ["app.py", "new.py", "gone.py"]
    assert files[1].is_new is True
    assert files[2].is_deleted is True
    lines = valid_line_map(files)
    assert lines["app.py"] == {2}
    assert lines["new.py"] == {1}
    assert "gone.py" not in lines
    assert text.count("diff --git") == 3


def test_truncated_diff_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/diffs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "old_path": "big.py",
                        "new_path": "big.py",
                        "too_large": True,
                        "diff": "",
                    }
                ],
            )
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=mr_payload(changes_count="1"))
        raise AssertionError(f"unexpected request {request.url}")

    client, _ = make_client(handler)
    with pytest.raises(GitLabAPIError) as exc:
        client.get_pull_diff("group/proj", 7)
    assert exc.value.status == 413


def test_truncated_diff_can_be_opted_out_with_warning():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/diffs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "old_path": "big.py",
                        "new_path": "big.py",
                        "too_large": True,
                        "diff": "",
                    },
                    {
                        "old_path": "ok.py",
                        "new_path": "ok.py",
                        "diff": "--- a/ok.py\n+++ b/ok.py\n@@ -1,1 +1,2 @@\n x\n+y\n",
                    },
                ],
            )
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=mr_payload(changes_count="2"))
        raise AssertionError(f"unexpected request {request.url}")

    client, _ = make_client(handler, GITLAB_ALLOW_TRUNCATED_DIFF="true")
    text = client.get_pull_diff("group/proj", 7)
    assert "ok.py" in text
    assert client._local.diff_warnings


def test_raw_diffs_fallback_when_diffs_endpoint_is_unavailable():
    raw = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x\n+y\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/diffs"):
            return httpx.Response(404, json={"message": "404 Not Found"})
        if request.url.path.endswith("/raw_diffs"):
            return httpx.Response(200, text=raw)
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=mr_payload(changes_count="1"))
        raise AssertionError(f"unexpected request {request.url}")

    client, _ = make_client(handler)
    assert client.get_pull_diff("group/proj", 7) == raw


def test_diff_not_ready_returns_409_after_retries():
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/diffs"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=mr_payload(changes_count="1"))
        raise AssertionError(f"unexpected request {request.url}")

    client = GitLabClient(
        gl_settings(),
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: sleeps.append(seconds),
        clock=lambda: 1_000,
    )
    with pytest.raises(GitLabAPIError) as exc:
        client.get_pull_diff("group/proj", 7)
    assert exc.value.status == 409
    assert len(sleeps) == 4


def test_compare_reconstructs_and_rejects_timeout():
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert "repository/compare" in str(request.url)
        return httpx.Response(
            200,
            json={
                "compare_timeout": False,
                "diffs": [
                    {
                        "old_path": "app.py",
                        "new_path": "app.py",
                        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x\n+y\n",
                    }
                ],
            },
        )

    client, _ = make_client(ok_handler)
    text = client.get_compare_diff("group/proj", "a", "b")
    assert "diff --git a/app.py b/app.py" in text

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"compare_timeout": True, "diffs": []})

    client, _ = make_client(timeout_handler)
    with pytest.raises(GitLabAPIError) as exc:
        client.get_compare_diff("group/proj", "a", "b")
    assert exc.value.status == 413


def test_list_reviews_filters_system_notes_and_paginates():
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "2":
            return httpx.Response(
                200,
                json=[{"id": 2, "body": "second", "author": {"username": "bob"}}],
            )
        return httpx.Response(
            200,
            headers={"X-Next-Page": "2"},
            json=[
                {"id": 1, "body": "first", "author": {"username": "alice"}},
                {"id": 3, "body": "system", "system": True},
            ],
        )

    client, requests = make_client(handler)
    reviews = client.list_reviews("group/proj", 7)
    assert [item["id"] for item in reviews] == [1, 2]
    assert len(requests) == 2


def test_archive_size_cap_and_success():
    def big_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": str(10 * 1024 * 1024)}
        )

    client, _ = make_client(big_handler, REPO_MAX_MB="1")
    with pytest.raises(GitLabAPIError) as exc:
        client.download_tarball("group/proj", "head")
    assert exc.value.status == 413

    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("sha") == "head"
        return httpx.Response(200, content=b"tarball-bytes")

    client, _ = make_client(ok_handler)
    assert client.download_tarball("group/proj", "head") == b"tarball-bytes"


def test_cross_origin_redirect_is_refused_and_token_not_replayed():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302, headers={"Location": "https://evil.example/steal"}
        )

    client, _ = make_client(handler)
    with pytest.raises(GitLabAPIError) as exc:
        client.get_pull("group/proj", 7)
    assert exc.value.status == 502
    assert len(requests) == 1
    assert requests[0].headers["PRIVATE-TOKEN"] == "glpat-test"


def test_same_origin_redirect_keeps_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "Location": "https://gitlab.com/api/v4/projects/group%2Fproj/merge_requests/7"
                },
            )
        return httpx.Response(200, json=mr_payload())

    client, _ = make_client(handler)
    assert client.get_pull("group/proj", 7).number == 7
    assert len(requests) == 2
    assert requests[1].headers["PRIVATE-TOKEN"] == "glpat-test"


def test_cross_origin_pagination_link_is_refused():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Link": '<https://evil.example/page2>; rel="next"'},
            json=[{"id": 1, "body": "x"}],
        )

    client, _ = make_client(handler)
    with pytest.raises(GitLabAPIError) as exc:
        client.list_reviews("group/proj", 7)
    assert exc.value.status == 502
    assert len(requests) == 1


def test_tarball_cross_origin_redirect_drops_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"Location": "https://objects.example/signed"}
            )
        assert "PRIVATE-TOKEN" not in request.headers
        return httpx.Response(200, content=b"tarball-bytes")

    client, _ = make_client(handler)
    assert client.download_tarball("group/proj", "head") == b"tarball-bytes"
    assert len(requests) == 2
    assert "PRIVATE-TOKEN" in requests[0].headers


def test_429_retries_and_401_does_not():
    calls = {"count": 0}

    def retry_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})
        return httpx.Response(200, json=mr_payload())

    client, _ = make_client(retry_handler)
    assert client.get_pull("group/proj", 7).number == 7
    assert calls["count"] == 2

    def auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "401 Unauthorized"})

    client, requests = make_client(auth_handler)
    with pytest.raises(GitLabAuthError):
        client.get_pull("group/proj", 7)
    assert len(requests) == 1
