"""GitLab settings, capability checks and allowlist semantics."""
from __future__ import annotations

import pytest

from codereview.config import Settings
from codereview.errors import ConfigError


def test_gitlab_defaults():
    settings = Settings.from_env({})
    assert settings.gitlab_base_url == "https://gitlab.com"
    assert settings.gitlab_api_base() == "https://gitlab.com/api/v4"
    assert settings.scm_provider == "github"
    assert settings.has_gitlab_write() is False
    assert settings.has_gitlab_webhook() is False


def test_gitlab_env_parsing_and_api_url():
    settings = Settings.from_env(
        {
            "SCM_PROVIDER": "gitlab",
            "GITLAB_BASE_URL": "https://gitlab.example.com/",
            "GITLAB_API_URL": "https://gitlab.example.com/gitlab/api/v4/",
            "GITLAB_TOKEN": "glpat-test",
            "GITLAB_WEBHOOK_SIGNING_SECRET": "whsec_test",
            "GITLAB_TIMEOUT": "12",
            "GITLAB_ALLOW_TRUNCATED_DIFF": "true",
        }
    )
    assert settings.scm_provider == "gitlab"
    assert settings.gitlab_base_url == "https://gitlab.example.com"
    assert settings.gitlab_api_base() == "https://gitlab.example.com/gitlab/api/v4"
    assert settings.has_gitlab_write() is True
    assert settings.has_gitlab_webhook() is True
    assert settings.gitlab_timeout == 12
    assert settings.gitlab_allow_truncated_diff is True


def test_invalid_provider_and_base_url():
    with pytest.raises(ConfigError):
        Settings.from_env({"SCM_PROVIDER": "bitbucket"})
    with pytest.raises(ConfigError):
        Settings.from_env({"GITLAB_BASE_URL": "gitlab.com"})


def test_gitlab_stats_redacted():
    settings = Settings.from_env(
        {
            "GITLAB_TOKEN": "glpat-secret",
            "GITLAB_WEBHOOK_SECRET": "hook-secret",
            "GITLAB_WEBHOOK_SIGNING_SECRET": "whsec_secret",
        }
    )
    redacted = settings.redacted()
    assert redacted["gitlab_token"] == "***"
    assert redacted["gitlab_webhook_secret"] == "***"
    assert redacted["gitlab_webhook_signing_secret"] == "***"


def test_require_webhook_gitlab_only():
    settings = Settings.from_env(
        {
            "GITLAB_TOKEN": "glpat-test",
            "GITLAB_WEBHOOK_SIGNING_SECRET": "whsec_test",
        }
    )
    settings.require_webhook()

    missing_token = Settings.from_env(
        {"GITLAB_WEBHOOK_SECRET": "hook-secret"}
    )
    with pytest.raises(ConfigError):
        missing_token.require_webhook()


def test_partial_provider_config_fails_fast():
    settings = Settings.from_env(
        {
            "GITHUB_TOKEN": "ghp-test",
            "GITHUB_WEBHOOK_SECRET": "github-secret-123456",
            "GITLAB_WEBHOOK_SECRET": "gitlab-secret-123456",
        }
    )
    with pytest.raises(ConfigError):
        settings.require_webhook()


def test_gitlab_repo_allowlist_accepts_bare_or_qualified():
    bare = Settings.from_env(
        {"GITLAB_BASE_URL": "https://gitlab.com", "REVIEW_ALLOWED_REPOS": "group/proj"}
    )
    assert bare.repo_allowed("gitlab.com/group/proj") is True
    assert bare.repo_allowed("gitlab.com/other/proj") is False
    # A bare GitHub repo with the same path is still matched literally; that is
    # the user's explicit allowlist entry, not a GitLab host shortcut.
    assert bare.repo_allowed("group/proj") is True

    qualified = Settings.from_env(
        {
            "GITLAB_BASE_URL": "https://gitlab.com",
            "REVIEW_ALLOWED_REPOS": "gitlab.com/group/proj",
        }
    )
    assert qualified.repo_allowed("gitlab.com/group/proj") is True
    assert qualified.repo_allowed("group/proj") is False
