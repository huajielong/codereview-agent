"""Typed errors. Every layer raises these instead of bare RuntimeError."""


class CodeReviewError(RuntimeError):
    """Base error for this package."""


class ConfigError(CodeReviewError):
    """Invalid or missing configuration."""


class LLMError(CodeReviewError):
    """LLM call failed (after retries)."""


class EmbeddingError(CodeReviewError):
    """Embedding call failed (after retries)."""


class GitHubError(CodeReviewError):
    """Base GitHub error."""


class GitHubAuthError(GitHubError):
    """GitHub App JWT / installation token failure."""


class GitHubAPIError(GitHubError):
    def __init__(self, status: int, message: str, url: str = ""):
        self.status = status
        self.url = url
        super().__init__(f"GitHub API {status}: {message}")


class WebhookError(CodeReviewError):
    """Signature or payload problem."""


class IndexError_(CodeReviewError):
    """Repository index build failure."""


class PublishError(CodeReviewError):
    """Review publishing failure."""
