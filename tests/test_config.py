"""Settings parsing and capability checks (GitHub side)."""
from __future__ import annotations

import pytest

from codereview.config import Settings
from codereview.errors import ConfigError


def test_defaults_are_github_and_dry_run():
    settings = Settings.from_env({})
    assert settings.mode == "security"
    assert settings.dry_run is True
    assert settings.github_api_url == "https://api.github.com"
    assert settings.has_github_write() is False
    assert settings.repo_allowed("owner/repo") is True


def test_fingerprint_is_stable_and_model_sensitive():
    base = Settings.from_env({"OPENAI_MODEL": "a", "OPENAI_API_KEY": "k"})
    same = Settings.from_env({"OPENAI_MODEL": "a", "OPENAI_API_KEY": "k"})
    other = Settings.from_env({"OPENAI_MODEL": "b", "OPENAI_API_KEY": "k"})
    assert base.fingerprint() == same.fingerprint()
    assert base.fingerprint() != other.fingerprint()


def test_invalid_mode_and_severity_raise():
    with pytest.raises(ConfigError):
        Settings.from_env({"REVIEW_MODE": "unknown"})
    with pytest.raises(ConfigError):
        Settings.from_env({"REVIEW_MIN_SEVERITY": "critical"})
    with pytest.raises(ConfigError):
        Settings.from_env({"GITHUB_INSTALLATION_ID": "not-an-int"})


def test_repo_allowlist_is_case_insensitive_and_exact():
    settings = Settings.from_env({"REVIEW_ALLOWED_REPOS": "Owner/Repo, other/repo"})
    assert settings.repo_allowed("owner/repo") is True
    assert settings.repo_allowed("OWNER/REPO") is True
    assert settings.repo_allowed("owner/other") is False
    assert settings.repo_allowed("other/repo2") is False


def test_redacted_masks_secrets():
    settings = Settings.from_env(
        {
            "GITHUB_TOKEN": "ghp_secret",
            "GITHUB_WEBHOOK_SECRET": "whsec_secret_123456",
            "OPENAI_API_KEY": "sk_secret",
            "COHERE_API_KEY": "co_secret",
        }
    )
    redacted = settings.redacted()
    assert redacted["github_token"] == "***"
    assert redacted["github_webhook_secret"] == "***"
    assert redacted["openai_api_key"] == "***"
    assert redacted["cohere_api_key"] == "***"
    assert redacted["allowed_repos"] == []


def test_require_llm():
    with pytest.raises(ConfigError):
        Settings.from_env({}).require_llm()
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENAI_MODEL": "m"}).require_llm()
    Settings.from_env({"OPENAI_MODEL": "m", "OPENAI_API_KEY": "k"}).require_llm()


def test_has_github_write_from_app_or_pat():
    assert Settings.from_env({"GITHUB_TOKEN": "t"}).has_github_write() is True
    assert (
        Settings.from_env(
            {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY_PATH": "key.pem"}
        ).has_github_write()
        is True
    )
    assert Settings.from_env({}).has_github_write() is False


def test_require_webhook_github(settings):
    # settings fixture has no webhook secret / write credentials
    with pytest.raises(ConfigError):
        settings.require_webhook()
    configured = Settings.from_env(
        {"GITHUB_WEBHOOK_SECRET": "x" * 20, "GITHUB_TOKEN": "t"}
    )
    configured.require_webhook()
