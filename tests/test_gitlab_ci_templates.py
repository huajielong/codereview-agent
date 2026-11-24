"""Regression checks for the GitLab CI hardenings (no YAML dependency)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_agent_ci_does_not_use_gitlab_cache():
    text = read(".gitlab-ci.yml")
    assert 'PIP_NO_CACHE_DIR: "1"' in text
    assert "\n  cache:" not in text


def test_templates_disable_cache_for_protected_push_audit():
    for path in ("ci/gitlab-codereview.yml", "docs/gitlab-codereview.example.yml"):
        text = read(path)
        assert "cache: []" in text
        assert 'PIP_NO_CACHE_DIR: "1"' in text
        assert 'case "$CODEREVIEW_AGENT_REF" in main|master' not in text
        assert "grep -Eq" in text
        assert "CODEREVIEW_ALLOW_MUTABLE_AGENT_REF" in text
