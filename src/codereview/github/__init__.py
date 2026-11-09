"""GitHub integration: App auth, REST client and webhook parsing."""

from codereview.github.auth import GitHubAppAuth, StaticTokenProvider, build_token_provider
from codereview.github.client import GitHubClient
from codereview.github.webhook import (
    PullRequestEvent,
    parse_pull_request_event,
    should_review,
    verify_signature,
)

__all__ = [
    "GitHubAppAuth",
    "StaticTokenProvider",
    "build_token_provider",
    "GitHubClient",
    "PullRequestEvent",
    "parse_pull_request_event",
    "should_review",
    "verify_signature",
]
