"""Small retry helper shared by LLM, embedding and GitHub clients.

Kept dependency-free so tests can run without tenacity.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def is_transient_status(status: int) -> bool:
    return status in (408, 409, 425, 429) or status >= 500


def call_with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.5,
    max_delay: float = 20.0,
    should_retry: Callable[[Exception], bool],
    on_retry: Callable[[int, Exception, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn``, retrying transient failures with linear backoff.

    ``attempts`` counts total tries (1 = no retry).
    """
    attempts = max(1, attempts)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - predicate decides
            last_exc = exc
            if attempt >= attempts or not should_retry(exc):
                raise
            delay = min(max_delay, base_delay * attempt)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
    assert last_exc is not None
    raise last_exc  # pragma: no cover - unreachable


def retry_after_seconds(value: str | None, default: float = 5.0) -> float:
    """Parse GitHub's ``Retry-After`` (seconds) defensively."""
    if not value:
        return default
    try:
        return max(0.0, min(60.0, float(value)))
    except (TypeError, ValueError):
        return default
