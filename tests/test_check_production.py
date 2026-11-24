"""Provider-aware offline readiness checks."""
from __future__ import annotations

from scripts.check_production import check

from codereview.config import Settings


def settings(tmp_path, **overrides) -> Settings:
    env = {
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "REVIEW_CONTEXT_ENABLED": "false",
        "DATABASE_PATH": str(tmp_path / "db.sqlite"),
        "CHROMA_PATH": str(tmp_path / "chroma"),
        "CACHE_DIR": str(tmp_path / "cache"),
        "REVIEW_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
        "REVIEW_HISTORY_PATH": str(tmp_path / "history.jsonl"),
        "REVIEW_PROMPT_DIR": str(tmp_path / "prompts"),
        "REVIEW_POLICY_PATH": str(tmp_path / "policy.json"),
        "GITLAB_BASE_URL": "https://gitlab.com",
    }
    env.update(overrides)
    return Settings.from_env(env)


def test_gitlab_provider_does_not_require_github_credentials(tmp_path):
    cfg = settings(
        tmp_path,
        GITLAB_TOKEN="glpat-test",
        GITLAB_WEBHOOK_SIGNING_SECRET="whsec_test",
    )
    report = check(cfg, "gitlab")
    assert report["ready"] is True
    names = {item["name"] for item in report["checks"]}
    assert "gitlab_write_auth" in names
    assert "gitlab_webhook_auth" in names
    assert "webhook_secret" not in names
    assert "github_write_auth" not in names


def test_github_provider_still_reports_missing_github_credentials(tmp_path):
    cfg = settings(tmp_path)
    report = check(cfg, "github")
    assert report["ready"] is False
    assert "webhook_secret" in report["failed"]


def test_both_provider_checks_both_sides(tmp_path):
    cfg = settings(
        tmp_path,
        GITHUB_TOKEN="ghp-test",
        GITHUB_WEBHOOK_SECRET="github-secret-123456",
        GITLAB_TOKEN="glpat-test",
        GITLAB_WEBHOOK_SECRET="gitlab-secret-123456",
    )
    report = check(cfg, "both")
    assert report["ready"] is True
    names = {item["name"] for item in report["checks"]}
    assert {"webhook_secret", "github_write_auth", "gitlab_write_auth"} <= names
