"""Typed errors. Every layer raises these instead of bare RuntimeError."""


class CodeReviewError(RuntimeError):
    """Base error for this package."""


class ConfigError(CodeReviewError):
    """Invalid or missing configuration."""


class LLMError(CodeReviewError):
    """LLM call failed (after retries)."""


class EmbeddingError(CodeReviewError):
    """Embedding call failed (after retries)."""


class SCMAPIError(CodeReviewError):
    """A source-control API call failed (provider-neutral)."""

    provider = "SCM"

    def __init__(self, status: int, message: str, url: str = ""):
        self.status = status
        self.url = url
        super().__init__(f"{self.provider} API {status}: {message}")


class GitHubError(CodeReviewError):
    """Base GitHub error."""


class GitHubAuthError(GitHubError):
    """GitHub App JWT / installation token failure."""


class GitHubAPIError(SCMAPIError, GitHubError):
    provider = "GitHub"


class GitLabError(CodeReviewError):
    """Base GitLab error."""


class GitLabAPIError(SCMAPIError, GitLabError):
    provider = "GitLab"


class GitLabAuthError(GitLabAPIError):
    def __init__(self, message: str, url: str = ""):
        super().__init__(401, message, url)


class WebhookError(CodeReviewError):
    """Signature or payload problem."""


class IndexError_(CodeReviewError):
    """Repository index build failure."""


class PublishError(CodeReviewError):
    """Review publishing failure."""
