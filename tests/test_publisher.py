"""Characterization tests for the GitHub publishing contract.

These must stay green while GitLab support is added: the GitHub 422 fallback,
the marker idempotency and the DB idempotency are all production behaviour.
"""
from __future__ import annotations

from dataclasses import replace

from codereview.errors import GitHubAPIError
from codereview.models import Finding
from codereview.review.publisher import Publisher, fingerprint_findings
from codereview.storage.db import Database

from tests.conftest import FakeGitHub

REPO = "owner/repo"
NUMBER = 7
HEAD = "head-sha"
FINDINGS = [Finding(issue="SQL injection", severity="high", file="app.py", line=3)]
VALID_LINES = {"app.py": {3, 4}}


def _publisher(settings, fake, db):
    return Publisher(settings, fake, db)


def _publish(publisher, *, dry_run: bool, findings=None, valid_lines=None):
    return publisher.publish(
        repo=REPO,
        number=NUMBER,
        head_sha=HEAD,
        findings=FINDINGS if findings is None else findings,
        valid_lines=VALID_LINES if valid_lines is None else valid_lines,
        mode="security",
        config_hash="cfg",
        dry_run=dry_run,
    )


def test_post_publishes_inline_comment_and_marker(settings, fake_github):
    db = Database(settings.db_path)
    try:
        result = _publish(_publisher(settings, fake_github, db), dry_run=False)
    finally:
        db.close()

    assert result.posted is True
    assert len(fake_github.created) == 1
    created = fake_github.created[0]
    assert created["repo"] == REPO
    assert created["number"] == NUMBER
    assert created["commit_id"] == HEAD
    assert created["comments"] == [
        {
            "path": "app.py",
            "line": 3,
            "side": "RIGHT",
            "body": result.inline_comments[0]["body"],
        }
    ]
    assert "<!-- codereview:" in created["body"]


def test_marker_in_existing_review_skips_and_repairs_db(settings, fake_github):
    fingerprint = fingerprint_findings(REPO, NUMBER, HEAD, "security", FINDINGS, "cfg")
    fake_github.reviews = [{"id": 55, "body": f"old\n<!-- codereview:{fingerprint} -->"}]
    db = Database(settings.db_path)
    try:
        result = _publish(_publisher(settings, fake_github, db), dry_run=False)
        assert result.posted is False
        assert "already posted" in (result.skipped_reason or "")
        assert fake_github.created == []
        assert db.is_posted(REPO, NUMBER, HEAD, fingerprint) is True
    finally:
        db.close()


def test_github_422_falls_back_to_summary_only(settings):
    fake = FakeGitHub(create_error=GitHubAPIError(422, "line is invalid"))
    db = Database(settings.db_path)
    try:
        result = _publish(_publisher(settings, fake, db), dry_run=False)
    finally:
        db.close()

    assert result.posted is True
    assert len(fake.created) == 1
    assert fake.created[0]["comments"] == []
    assert "Inline comments were dropped" in fake.created[0]["body"]


def test_db_idempotency_skips_second_post(settings, fake_github):
    db = Database(settings.db_path)
    try:
        publisher = _publisher(settings, fake_github, db)
        first = _publish(publisher, dry_run=False)
        second = _publish(publisher, dry_run=False)
        assert first.posted is True
        assert second.posted is False
        assert second.skipped_reason == "already posted (database)"
        assert len(fake_github.created) == 1
    finally:
        db.close()


def test_no_findings_and_empty_summary_disabled_skips(settings, fake_github):
    settings = replace(settings, post_empty_summary=False)
    db = Database(settings.db_path)
    try:
        result = _publish(
            _publisher(settings, fake_github, db),
            dry_run=False,
            findings=[],
            valid_lines={},
        )
    finally:
        db.close()

    assert result.posted is False
    assert result.skipped_reason == "no findings"
    assert fake_github.created == []


def test_dry_run_does_not_post(settings, fake_github):
    db = Database(settings.db_path)
    try:
        result = _publish(_publisher(settings, fake_github, db), dry_run=True)
    finally:
        db.close()

    assert result.posted is False
    assert result.skipped_reason == "dry-run"
    assert fake_github.created == []
