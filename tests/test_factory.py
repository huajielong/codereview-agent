"""Provider selection and end-to-end pipeline behaviour with fakes."""
from __future__ import annotations

from dataclasses import replace

from codereview.gitlab.client import GitLabClient
from codereview.github.client import GitHubClient
from codereview.review.factory import build_pipeline
from codereview.scm import ProviderRouter
from codereview.storage.db import Database

from tests.conftest import FakeGitLab, FakeLLM


def test_explicit_gitlab_provider_constructs_gitlab_client(settings):
    settings = replace(
        settings,
        scm_provider="gitlab",
        gitlab_token="glpat-test",
        openai_api_key="test-key",
        model="test-model",
    )
    db = Database(settings.db_path)
    try:
        pipeline = build_pipeline(settings, db=db, llm=FakeLLM(), provider="gitlab")
        assert isinstance(pipeline.github, GitLabClient)
        pipeline.github.close()
    finally:
        db.close()


def test_auto_uses_router_when_both_providers_have_write_credentials(settings):
    settings = replace(
        settings,
        github_token="ghp-test",
        gitlab_token="glpat-test",
        openai_api_key="test-key",
        model="test-model",
    )
    db = Database(settings.db_path)
    try:
        pipeline = build_pipeline(settings, db=db, llm=FakeLLM())
        assert isinstance(pipeline.github, ProviderRouter)
        assert set(pipeline.github.hosts) == {"github", "gitlab"}
        pipeline.github.close()
    finally:
        db.close()


def test_auto_uses_github_when_no_gitlab_credentials(settings):
    settings = replace(settings, openai_api_key="test-key", model="test-model")
    db = Database(settings.db_path)
    try:
        pipeline = build_pipeline(settings, db=db, llm=FakeLLM())
        assert isinstance(pipeline.github, GitHubClient)
        pipeline.github.close()
    finally:
        db.close()


def test_gitlab_pipeline_dry_run_end_to_end(settings):
    settings = replace(
        settings,
        scm_provider="gitlab",
        gitlab_token="glpat-test",
        dry_run=True,
        post_empty_summary=True,
    )
    fake = FakeGitLab()
    db = Database(settings.db_path)
    try:
        pipeline = build_pipeline(
            settings, db=db, github=fake, llm=FakeLLM(), provider="gitlab"
        )
        outcome = pipeline.review_pr("gitlab.com/group/proj", 3)
    finally:
        db.close()

    assert outcome.status == "completed"
    assert len(outcome.findings) == 1
    assert outcome.posted is False
    assert fake.created == []


def test_gitlab_pipeline_posts_end_to_end(settings):
    settings = replace(
        settings,
        scm_provider="gitlab",
        gitlab_token="glpat-test",
        dry_run=False,
        post_empty_summary=True,
    )
    fake = FakeGitLab()
    db = Database(settings.db_path)
    try:
        pipeline = build_pipeline(
            settings, db=db, github=fake, llm=FakeLLM(), provider="gitlab"
        )
        outcome = pipeline.review_pr("gitlab.com/group/proj", 3)
    finally:
        db.close()

    assert outcome.status == "completed"
    assert outcome.posted is True
    assert len(fake.created) == 1
    assert fake.created[0]["comments"][0]["line"] == 3
    assert "<!-- codereview:" in fake.created[0]["body"]
