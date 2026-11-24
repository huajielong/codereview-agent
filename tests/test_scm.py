"""Provider-neutral repo identity and routing."""
from __future__ import annotations

import pytest

from codereview.config import Settings
from codereview.errors import ConfigError
from codereview.scm import (
    ProviderRouter,
    coerce_provider,
    project_path,
    provider_for_repo,
    qualify_repo,
)

from tests.conftest import FakeGitHub, FakeGitLab


def test_qualify_github_keeps_existing_key():
    assert qualify_repo("github", "https://api.github.com", "owner/repo") == "owner/repo"
    assert (
        qualify_repo("github", "https://api.github.com", "https://github.com/owner/repo.git")
        == "owner/repo"
    )
    assert (
        qualify_repo("github", "https://api.github.com", "git@github.com:owner/repo.git")
        == "owner/repo"
    )


def test_qualify_gitlab_adds_host():
    assert (
        qualify_repo("gitlab", "https://gitlab.com", "group/proj")
        == "gitlab.com/group/proj"
    )
    assert (
        qualify_repo("gitlab", "https://gitlab.com/", "gitlab.com/group/sub/proj.git")
        == "gitlab.com/group/sub/proj"
    )
    assert (
        qualify_repo(
            "gitlab", "https://gitlab.com", "https://gitlab.com/group/proj"
        )
        == "gitlab.com/group/proj"
    )


def test_qualify_rejects_wrong_host_and_bad_paths():
    with pytest.raises(ConfigError):
        qualify_repo("gitlab", "https://gitlab.com", "https://evil.example/group/proj")
    with pytest.raises(ConfigError):
        qualify_repo("gitlab", "https://gitlab.com", "proj")
    with pytest.raises(ConfigError):
        qualify_repo("github", "https://api.github.com", "https://gitlab.com/group/proj")


def test_project_path_and_provider_detection():
    settings = Settings.from_env({"GITLAB_BASE_URL": "https://gitlab.com"})
    assert project_path("gitlab.com/group/sub/proj", settings.gitlab_base_url) == "group/sub/proj"
    assert provider_for_repo("gitlab.com/group/proj", settings) == "gitlab"
    assert provider_for_repo("owner/repo", settings) == "github"
    assert coerce_provider("GitLab") == "gitlab"
    with pytest.raises(ConfigError):
        coerce_provider("bitbucket")


def test_provider_router_dispatches_by_repo():
    settings = Settings.from_env({"GITLAB_BASE_URL": "https://gitlab.com"})
    github = FakeGitHub()
    gitlab = FakeGitLab()
    router = ProviderRouter(settings, {"github": github, "gitlab": gitlab})

    assert router.get_pull("owner/repo", 1).repo == "owner/repo"
    assert router.get_pull("gitlab.com/group/proj", 3).repo == "gitlab.com/group/proj"
    assert router.get_pull_diff("gitlab.com/group/proj", 3) == gitlab.diff
    assert router.get_compare_diff("owner/repo", "a", "b") == github.compare_diff

    router.close()
    assert github.closed is True
    assert gitlab.closed is True
