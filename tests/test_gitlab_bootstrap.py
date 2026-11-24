"""Bootstrap API helpers (no real GitLab calls, no secrets printed)."""
from __future__ import annotations

import json
from urllib.parse import unquote

import httpx
import pytest

from scripts.gitlab_bootstrap import (
    _check,
    create_deploy_token,
    ensure_project,
    main,
    set_variable,
)


def client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://gitlab.test/api/v4",
        headers={"PRIVATE-TOKEN": "glpat-test"},
        transport=httpx.MockTransport(handler),
    )


def test_ensure_project_creates_missing_personal_project():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        calls.append((request.method, path))
        if request.method == "GET" and path.endswith("/projects/huajielong/demo"):
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        if request.method == "GET" and path.endswith("/user"):
            return httpx.Response(200, json={"id": 42, "username": "huajielong"})
        if request.method == "POST" and path.endswith("/projects"):
            body = json.loads(request.content)
            # Personal namespace must not send namespace_id (GitLab.com quirk).
            assert "namespace_id" not in body
            assert body["visibility"] == "private"
            return httpx.Response(201, json={"id": 9, "path_with_namespace": "huajielong/demo"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    c = client(handler)
    try:
        project = ensure_project(c, "huajielong/demo")
    finally:
        c.close()
    assert project["id"] == 9
    assert ("POST", "/api/v4/projects") in calls


def test_ensure_project_resolves_group_namespace():
    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.method == "GET" and path.endswith("/projects/team/demo"):
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        if request.method == "GET" and path.endswith("/user"):
            return httpx.Response(200, json={"id": 42, "username": "huajielong"})
        if request.method == "GET" and path.endswith("/namespaces"):
            assert request.url.params.get("search") == "team"
            return httpx.Response(200, json=[{"id": 7, "full_path": "team"}])
        if request.method == "POST" and path.endswith("/projects"):
            body = json.loads(request.content)
            assert body["namespace_id"] == 7
            return httpx.Response(201, json={"id": 10, "path_with_namespace": "team/demo"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    c = client(handler)
    try:
        project = ensure_project(c, "team/demo")
    finally:
        c.close()
    assert project["id"] == 10


def test_set_variable_masks_secret_keys():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"key": "GITLAB_TOKEN"})

    c = client(handler)
    try:
        set_variable(c, 9, "CODEREVIEW_GITLAB_TOKEN", "glpat-secret")
        set_variable(c, 9, "OPENAI_BASE_URL", "https://api.example")
    finally:
        c.close()

    assert seen[0]["masked"] is True
    assert seen[1]["masked"] is False


def test_set_variable_can_be_protected():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"key": "CODEREVIEW_GITLAB_TOKEN"})

    c = client(handler)
    try:
        set_variable(c, 9, "CODEREVIEW_GITLAB_TOKEN", "glpat-secret", protected=True)
    finally:
        c.close()

    assert seen[0]["protected"] is True
    assert seen[0]["masked"] is True


def test_set_review_token_requires_separate_token(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-bootstrap")
    assert main(["--set-review-token", "--yes"]) == 2


def test_review_token_file_missing_returns_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-bootstrap")
    missing = tmp_path / "missing-token"
    assert main([
        "--set-review-token",
        "--review-token-file",
        str(missing),
        "--yes",
    ]) == 2


def test_review_token_and_file_are_mutually_exclusive(monkeypatch, tmp_path):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-bootstrap")
    token_file = tmp_path / "review-token"
    token_file.write_text("glpat-review", encoding="utf-8")
    assert main([
        "--set-review-token",
        "--review-token",
        "glpat-review",
        "--review-token-file",
        str(token_file),
        "--yes",
    ]) == 2


def test_redirects_are_rejected_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/steal"},
        )

    c = client(handler)
    try:
        assert c.follow_redirects is False
        response = c.get("/user")
        with pytest.raises(SystemExit):
            _check(response, "GET /user")
    finally:
        c.close()


def test_create_deploy_token_returns_none_when_it_already_exists():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"name": "codereview-agent-clone", "scopes": ["read_repository"]}],
        )

    c = client(handler)
    try:
        assert create_deploy_token(c, 9) is None
    finally:
        c.close()
