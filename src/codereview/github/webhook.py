"""Webhook signature verification and pull_request event filtering."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from codereview.config import Settings

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass(frozen=True)
class PullRequestEvent:
    delivery_id: str
    action: str
    repo: str
    number: int
    installation_id: int | None
    head_sha: str
    base_sha: str
    title: str
    body: str
    draft: bool
    state: str
    merged: bool
    sender: str
    sender_type: str
    html_url: str = ""

    @property
    def is_bot(self) -> bool:
        return self.sender_type.lower() == "bot" or self.sender.endswith("[bot]")


def verify_signature(secret: str | bytes, body: bytes, signature: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256``."""
    if not signature:
        return False
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = "sha256=" + hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def parse_pull_request_event(
    event_name: str, payload: dict, delivery_id: str = ""
) -> PullRequestEvent | None:
    if event_name != "pull_request" or not isinstance(payload, dict):
        return None
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name") or ""
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    number = pr.get("number")
    if not repo or not number:
        return None

    installation_id = installation.get("id")
    return PullRequestEvent(
        delivery_id=delivery_id,
        action=payload.get("action") or "",
        repo=repo,
        number=int(pr.get("number")),
        installation_id=int(installation_id) if installation_id else None,
        head_sha=head.get("sha") or "",
        base_sha=base.get("sha") or "",
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        draft=bool(pr.get("draft")),
        state=pr.get("state") or "open",
        merged=bool(pr.get("merged")),
        sender=sender.get("login") or "",
        sender_type=sender.get("type") or "User",
        html_url=pr.get("html_url") or "",
    )


def should_review(event: PullRequestEvent, settings: Settings) -> tuple[bool, str]:
    """Return (decision, reason) for a parsed event."""
    if event.action not in REVIEW_ACTIONS:
        return False, f"action {event.action!r} is not reviewed"
    if event.state != "open" or event.merged:
        return False, "PR is not open"
    if event.draft and not settings.review_drafts:
        return False, "draft PR (set REVIEW_DRAFTS=true to review)"
    if event.is_bot and settings.skip_bots:
        return False, "bot author"
    if not settings.repo_allowed(event.repo):
        return False, "repository not in REVIEW_ALLOWED_REPOS"
    if not event.head_sha:
        return False, "missing head sha"
    return True, "ok"
