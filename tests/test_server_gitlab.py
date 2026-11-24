"""FastAPI /webhook/gitlab: verification, queueing and provider routing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from codereview.config import Settings
from codereview.server.app import create_app
from codereview.storage.db import Database

KEY = b"0123456789abcdef0123456789abcdef"
SIGNING_SECRET = "whsec_" + base64.b64encode(KEY).decode()


class FakePipeline:
    def review_pr(self, *args, **kwargs):  # pragma: no cover - workers are off
        raise AssertionError("workers must not run in these tests")


def settings(tmp_path, **overrides) -> Settings:
    env = {
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "REVIEW_CONTEXT_ENABLED": "false",
        "REVIEW_DRY_RUN": "true",
        "REVIEW_ALLOWED_REPOS": "",
        "DATABASE_PATH": str(tmp_path / "codereview.db"),
        "CHROMA_PATH": str(tmp_path / "chroma"),
        "CACHE_DIR": str(tmp_path / "cache"),
        "REVIEW_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
        "REVIEW_HISTORY_PATH": str(tmp_path / "history.jsonl"),
        "REVIEW_PROMPT_DIR": str(tmp_path / "prompts"),
        "REVIEW_POLICY_PATH": str(tmp_path / "policy.json"),
        "GITLAB_BASE_URL": "https://gitlab.com",
        "GITLAB_WEBHOOK_SECRET": "hook-secret-123456",
    }
    env.update(overrides)
    return Settings.from_env(env)


def payload(action: str = "open", **attrs_overrides) -> dict:
    attrs = {
        "iid": 5,
        "action": action,
        "state": "opened",
        "draft": False,
        "oldrev": None,
        "last_commit": {"id": "sha-5"},
        "url": "https://gitlab.com/group/proj/-/merge_requests/5",
        "title": "Add thing",
        "description": "body",
        "changes": {},
        "merged_at": None,
    }
    attrs.update(attrs_overrides)
    return {
        "object_kind": "merge_request",
        "project": {
            "id": 1,
            "path_with_namespace": "group/proj",
            "web_url": "https://gitlab.com/group/proj",
        },
        "object_attributes": attrs,
        "user": {"username": "alice", "bot": False},
    }


def make_app(tmp_path, **overrides):
    cfg = settings(tmp_path, **overrides)
    db = Database(cfg.db_path)
    app = create_app(
        cfg,
        pipeline=FakePipeline(),
        db=db,
        start_workers=False,
        enforce_config=False,
    )
    return app, db, cfg


def post_gitlab(client, body: dict, *, delivery: str = "msg-1", token: str = "hook-secret-123456", extra_headers=None):
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Token": token,
        "webhook-id": delivery,
        "Content-Type": "application/json",
    }
    headers.update(extra_headers or {})
    return client.post("/webhook/gitlab", content=json.dumps(body).encode(), headers=headers)


def test_queues_merge_request_job(tmp_path):
    app, db, _ = make_app(tmp_path)
    try:
        with TestClient(app) as client:
            response = post_gitlab(client, payload())
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        job = db.job("gitlab:msg-1")
        assert job is not None
        assert job["repo"] == "gitlab.com/group/proj"
        assert job["pr_number"] == 5
        assert job["installation_id"] is None
        assert job["action"] == "opened"
        assert job["head_sha"] == "sha-5"
    finally:
        db.close()


def test_duplicate_delivery_is_ignored(tmp_path):
    app, db, _ = make_app(tmp_path)
    try:
        with TestClient(app) as client:
            first = post_gitlab(client, payload(), delivery="msg-dup")
            second = post_gitlab(client, payload(), delivery="msg-dup")
        assert first.status_code == 202
        assert second.json()["status"] == "duplicate"
        assert db.job("gitlab:msg-dup") is not None
    finally:
        db.close()


def test_bad_token_rejected(tmp_path):
    app, db, _ = make_app(tmp_path)
    try:
        with TestClient(app) as client:
            response = post_gitlab(client, payload(), token="wrong")
        assert response.status_code == 401
        assert db.job("gitlab:msg-1") is None
    finally:
        db.close()


def test_label_only_update_is_skipped(tmp_path):
    app, db, _ = make_app(tmp_path)
    try:
        with TestClient(app) as client:
            response = post_gitlab(client, payload("update"), delivery="msg-label")
        assert response.status_code == 202
        assert response.json()["status"] == "skipped"
        assert db.job("gitlab:msg-label") is None
    finally:
        db.close()


def test_draft_and_missing_commit_are_skipped(tmp_path):
    app, db, _ = make_app(tmp_path)
    try:
        with TestClient(app) as client:
            draft = post_gitlab(
                client, payload("open", draft=True), delivery="msg-draft"
            )
            no_commit = post_gitlab(
                client, payload("open", last_commit=None), delivery="msg-no-commit"
            )
        assert draft.json()["status"] == "skipped"
        assert no_commit.json()["status"] == "skipped"
        assert db.job("gitlab:msg-draft") is None
        assert db.job("gitlab:msg-no-commit") is None
    finally:
        db.close()


def test_signing_secret_is_strict_and_does_not_downgrade(tmp_path):
    app, db, _ = make_app(
        tmp_path,
        GITLAB_WEBHOOK_SECRET="",
        GITLAB_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
    )
    body = json.dumps(payload()).encode()
    timestamp = str(int(time.time()))
    message = f"msg-sig.{timestamp}.".encode() + body
    signature = "v1," + base64.b64encode(
        hmac.new(KEY, message, hashlib.sha256).digest()
    ).decode()
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "webhook-id": "msg-sig",
        "webhook-timestamp": timestamp,
        "webhook-signature": signature,
        "Content-Type": "application/json",
    }
    try:
        with TestClient(app) as client:
            good = client.post("/webhook/gitlab", content=body, headers=headers)
            legacy = client.post(
                "/webhook/gitlab",
                content=body,
                headers={
                    "X-Gitlab-Event": "Merge Request Hook",
                    "X-Gitlab-Token": "hook-secret-123456",
                    "webhook-id": "msg-legacy",
                    "Content-Type": "application/json",
                },
            )
        assert good.status_code == 202
        assert legacy.status_code == 401
    finally:
        db.close()


def test_routes_are_enabled_per_provider(tmp_path):
    gitlab_only, db, _ = make_app(tmp_path)
    try:
        with TestClient(gitlab_only) as client:
            assert client.post("/webhook", content=b"{}").status_code == 404
    finally:
        db.close()

    github_only, db2, _ = make_app(
        tmp_path,
        GITLAB_WEBHOOK_SECRET="",
        GITHUB_WEBHOOK_SECRET="github-secret-123456",
        GITHUB_TOKEN="ghp-test",
    )
    try:
        with TestClient(github_only) as client:
            assert client.post("/webhook/gitlab", content=b"{}").status_code == 404
    finally:
        db2.close()
