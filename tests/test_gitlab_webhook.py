"""GitLab webhook verification, event mapping and dedupe keys."""
from __future__ import annotations

import base64
import hashlib
import hmac

from codereview.config import Settings
from codereview.github.webhook import should_review
from codereview.gitlab.webhook import (
    delivery_id,
    is_bot_user,
    parse_merge_request_event,
    verify_request,
    verify_secret_token,
    verify_signing_token,
)

KEY = b"0123456789abcdef0123456789abcdef"
SIGNING_SECRET = "whsec_" + base64.b64encode(KEY).decode()


def sign(body: bytes, *, message_id: str, timestamp: str, key: bytes = KEY) -> str:
    message = f"{message_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def settings(**overrides) -> Settings:
    env = {
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "GITLAB_BASE_URL": "https://gitlab.com",
    }
    env.update(overrides)
    return Settings.from_env(env)


def payload(action: str = "open", **attrs_overrides) -> dict:
    attrs = {
        "iid": 5,
        "action": action,
        "state": "opened",
        "draft": False,
        "work_in_progress": False,
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


def test_secret_token_compare():
    assert verify_secret_token("secret", "secret") is True
    assert verify_secret_token("secret", "other") is False
    assert verify_secret_token("", "secret") is False
    assert verify_secret_token("secret", None) is False


def test_signing_token_valid_invalid_and_multiple_signatures():
    body = b'{"hello":"world"}'
    timestamp = "1000"
    signature = sign(body, message_id="msg-1", timestamp=timestamp)

    assert verify_signing_token(
        SIGNING_SECRET, body, "msg-1", timestamp, signature, now=1000
    ) is True
    assert verify_signing_token(
        SIGNING_SECRET, b'{"tampered":1}', "msg-1", timestamp, signature, now=1000
    ) is False
    assert verify_signing_token(
        SIGNING_SECRET, body, "msg-1", timestamp, signature, now=2000
    ) is False
    assert verify_signing_token(
        SIGNING_SECRET, body, "msg-1", timestamp, "v1,bad " + signature, now=1000
    ) is True
    assert verify_signing_token(
        "not-a-whsec", body, "msg-1", timestamp, signature, now=1000
    ) is False


def test_verify_request_does_not_downgrade_to_token():
    body = b"{}"
    headers = {
        "X-Gitlab-Token": "legacy-secret",
        "webhook-id": "msg-1",
        "webhook-timestamp": "1000",
        "webhook-signature": "v1,bad",
    }
    strict = settings(
        GITLAB_WEBHOOK_SECRET="legacy-secret",
        GITLAB_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
    )
    ok, reason = verify_request(strict, headers, body, now=1000)
    assert ok is False
    assert reason == "signature"

    legacy = settings(GITLAB_WEBHOOK_SECRET="legacy-secret")
    ok, reason = verify_request(legacy, headers, body)
    assert ok is True
    assert reason == "token"

    ok, _ = verify_request(legacy, {"X-Gitlab-Token": "wrong"}, body)
    assert ok is False


def test_delivery_id_prefers_webhook_id_then_idempotency_key():
    body = b"{}"
    assert delivery_id({"webhook-id": "m1"}, body) == "gitlab:m1"
    assert delivery_id({"Idempotency-Key": "m2"}, body) == "gitlab:m2"
    hashed = delivery_id({}, body)
    assert hashed.startswith("gitlab:")
    assert hashed == delivery_id({}, body)
    # Recursive hooks share X-Gitlab-Event-UUID; it must not drive dedupe.
    assert delivery_id({"X-Gitlab-Event-UUID": "shared"}, body) != "gitlab:shared"


def test_parse_open_merge_request():
    event = parse_merge_request_event(
        "Merge Request Hook", payload(), "gitlab:m1", "https://gitlab.com"
    )
    assert event is not None
    assert event.action == "opened"
    assert event.repo == "gitlab.com/group/proj"
    assert event.number == 5
    assert event.head_sha == "sha-5"
    assert event.state == "open"
    assert event.draft is False
    assert event.sender == "alice"
    assert event.sender_type == "User"
    assert should_review(event, settings())[0] is True


def test_update_actions_and_label_only_skip():
    push = parse_merge_request_event(
        "Merge Request Hook", payload("update", oldrev="old-sha")
    )
    assert push.action == "synchronize"

    label_only = parse_merge_request_event("Merge Request Hook", payload("update"))
    assert label_only.action == "update"
    assert should_review(label_only, settings())[0] is False

    ready = parse_merge_request_event(
        "Merge Request Hook",
        payload(
            "update",
            changes={"draft": {"previous": True, "current": False}},
        ),
    )
    assert ready.action == "ready_for_review"

    approved = parse_merge_request_event("Merge Request Hook", payload("approved"))
    assert approved.action == "approved"
    assert should_review(approved, settings())[0] is False


def test_draft_and_bot_and_missing_commit():
    draft = parse_merge_request_event("Merge Request Hook", payload("open", draft=True))
    assert draft.draft is True
    assert should_review(draft, settings())[0] is False

    bot_payload = payload()
    bot_payload["user"] = {"username": "project_1_bot_abc", "bot": True}
    bot = parse_merge_request_event("Merge Request Hook", bot_payload)
    assert bot.sender_type == "Bot"
    assert is_bot_user({"username": "project_1_bot_abc"}) is True
    assert is_bot_user({"username": "alice"}) is False

    no_commit = parse_merge_request_event(
        "Merge Request Hook", payload("open", last_commit=None)
    )
    assert no_commit.head_sha == ""
    assert should_review(no_commit, settings())[0] is False


def test_wrong_event_or_missing_project_is_ignored():
    assert parse_merge_request_event("Push Hook", payload()) is None
    assert parse_merge_request_event("Merge Request Hook", {"object_kind": "push"}) is None
    assert parse_merge_request_event(
        "Merge Request Hook",
        {"object_kind": "merge_request", "object_attributes": {"iid": 1}},
    ) is None
