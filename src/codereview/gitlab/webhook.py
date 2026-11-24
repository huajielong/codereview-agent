"""GitLab webhook verification and Merge Request event parsing.

Two authentication schemes are supported:

* ``X-Gitlab-Token``: a shared secret sent as plain text (legacy).
* ``webhook-signature``: Standard Webhooks HMAC-SHA256 (recommended).

If a signing secret is configured, the token header is **never** accepted as a
fallback; a failed signature is a hard 401. This avoids downgrading a strong
configuration back to the plaintext token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlparse

from codereview.config import Settings
from codereview.errors import ConfigError
from codereview.github.webhook import PullRequestEvent
from codereview.scm import qualify_repo

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
_WHSEC_PREFIX = "whsec_"


def _header(headers, name: str) -> str | None:
    for key in (name, name.lower(), name.upper()):
        try:
            value = headers.get(key)
        except AttributeError:
            value = None
        if value is not None:
            return value
    return None


def verify_secret_token(secret: str | None, token: str | None) -> bool:
    if not secret or not token:
        return False
    return hmac.compare_digest(secret, token)


def verify_signing_token(
    secret: str | None,
    body: bytes,
    webhook_id: str | None,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
    tolerance: int = 300,
) -> bool:
    """Verify a GitLab Standard Webhooks signature.

    ``webhook-signature`` is a space-separated list of ``v1,<base64>`` values.
    The signed message is ``"{webhook-id}.{webhook-timestamp}.{raw body}"``.
    """
    if not (secret and webhook_id and timestamp and signature):
        return False
    if not secret.startswith(_WHSEC_PREFIX):
        return False
    try:
        timestamp_value = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp_value) > tolerance:
        return False

    encoded_key = secret[len(_WHSEC_PREFIX):].strip()
    try:
        key = base64.b64decode(encoded_key + "=" * (-len(encoded_key) % 4), validate=False)
    except Exception:  # noqa: BLE001 - malformed secret is a verification failure
        try:
            key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
        except Exception:  # noqa: BLE001
            return False
    if not key:
        return False

    message = f"{webhook_id}.{timestamp}.".encode("utf-8") + (body or b"")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    expected = "v1," + base64.b64encode(digest).decode("ascii")
    for part in str(signature).split():
        if hmac.compare_digest(expected, part.strip()):
            return True
    return False


def verify_request(
    settings: Settings,
    headers,
    body: bytes,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)``; signing secret takes precedence, no downgrade."""
    if settings.gitlab_webhook_signing_secret:
        ok = verify_signing_token(
            settings.gitlab_webhook_signing_secret,
            body,
            _header(headers, "webhook-id"),
            _header(headers, "webhook-timestamp"),
            _header(headers, "webhook-signature"),
            now=now,
        )
        return ok, "signature"
    if settings.gitlab_webhook_secret:
        ok = verify_secret_token(
            settings.gitlab_webhook_secret,
            _header(headers, "X-Gitlab-Token"),
        )
        return ok, "token"
    return False, "not_configured"


def delivery_id(headers, body: bytes) -> str:
    """Stable idempotency key. Never use X-Gitlab-Event-UUID (recursive hooks)."""
    for name in ("webhook-id", "Idempotency-Key"):
        value = _header(headers, name)
        if value:
            return f"gitlab:{value}"
    return f"gitlab:{hashlib.sha256(body or b'').hexdigest()[:32]}"


def is_bot_user(user: dict) -> bool:
    if bool(user.get("bot")):
        return True
    username = str(user.get("username") or "").lower()
    return (
        username.endswith(".bot")
        or username.endswith("_bot")
        or "_bot_" in username
    )


def _draft_became_ready(attrs: dict, changes: dict) -> bool:
    if attrs.get("draft") or attrs.get("work_in_progress"):
        return False
    for key in ("draft", "work_in_progress"):
        change = changes.get(key)
        if isinstance(change, dict):
            if change.get("previous") and not change.get("current"):
                return True
        elif change:
            # Some payloads expose a scalar change; only trust it when the
            # current value is false (checked above).
            return True
    return False


def _map_action(attrs: dict) -> str:
    action = str(attrs.get("action") or "")
    if action == "open":
        return "opened"
    if action == "reopen":
        return "reopened"
    if action == "update":
        if attrs.get("oldrev"):
            return "synchronize"
        if _draft_became_ready(attrs, attrs.get("changes") or {}):
            return "ready_for_review"
        return "update"
    return action


def _project_path(project: dict) -> str:
    path = str(project.get("path_with_namespace") or "").strip()
    if path:
        return path
    web_url = str(project.get("web_url") or "").strip()
    if web_url:
        return urlparse(web_url).path.strip("/")
    return ""


def parse_merge_request_event(
    event_name: str,
    payload: dict,
    delivery: str = "",
    base_url: str = "https://gitlab.com",
) -> PullRequestEvent | None:
    if event_name != "Merge Request Hook" or not isinstance(payload, dict):
        return None
    if payload.get("object_kind") not in (None, "merge_request"):
        return None

    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    path = _project_path(project)
    if not path:
        return None
    try:
        repo = qualify_repo("gitlab", base_url, path)
    except ConfigError:
        return None

    iid = attrs.get("iid")
    if not iid:
        return None
    try:
        number = int(iid)
    except (TypeError, ValueError):
        return None

    state = str(attrs.get("state") or "opened")
    merged = state == "merged" or bool(attrs.get("merged_at"))
    state_value = "open" if state in ("opened", "locked") and not merged else state
    last_commit = attrs.get("last_commit") or {}
    user = payload.get("user") or {}
    bot = is_bot_user(user)

    return PullRequestEvent(
        delivery_id=delivery,
        action=_map_action(attrs),
        repo=repo,
        number=number,
        installation_id=None,
        head_sha=last_commit.get("id") or "",
        base_sha=(attrs.get("diff_refs") or {}).get("base_sha") or "",
        title=attrs.get("title") or "",
        body=attrs.get("description") or "",
        draft=bool(attrs.get("draft") or attrs.get("work_in_progress")),
        state=state_value,
        merged=merged,
        sender=user.get("username") or "",
        sender_type="Bot" if bot else "User",
        html_url=attrs.get("url") or "",
    )


__all__ = [
    "REVIEW_ACTIONS",
    "delivery_id",
    "is_bot_user",
    "parse_merge_request_event",
    "verify_request",
    "verify_secret_token",
    "verify_signing_token",
]
