"""OpenAI-compatible chat client with retries and token accounting."""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from codereview.config import Settings
from codereview.errors import LLMError
from codereview.logging_utils import get_logger
from codereview.retry import call_with_retries

log = get_logger(__name__)
_MAX_INPUT_CHARS = 200_000


@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    latency_ms: int = 0


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if type(exc).__name__ in (
        "APIConnectionError", "APITimeoutError", "InternalServerError",
        "RateLimitError", "ServiceUnavailableError", "APIStatusError",
    ):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


class LLMClient:
    def __init__(self, settings: Settings, client=None, sleep=time.sleep):
        self.settings = settings
        self._sleep = sleep
        self._client = client
        if self._client is None:
            settings.require_llm()
            from openai import OpenAI

            self._client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                timeout=settings.llm_timeout,
            )

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        if len(user) > _MAX_INPUT_CHARS:
            raise LLMError(f"prompt too large: {len(user)} chars (limit {_MAX_INPUT_CHARS})")

        started = time.monotonic()
        base_limit = max_tokens or self.settings.max_tokens

        def call(limit: int) -> LLMResponse:
            response = self._client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=limit,
            )
            choice = response.choices[0]
            message = choice.message
            content = message.content or ""
            usage = getattr(response, "usage", None)
            return LLMResponse(
                content=content,
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                model=getattr(response, "model", self.settings.model or ""),
                finish_reason=getattr(choice, "finish_reason", "") or "",
            )

        def run(limit: int) -> LLMResponse:
            return call_with_retries(
                lambda: call(limit),
                attempts=self.settings.llm_retries,
                should_retry=_is_transient,
                on_retry=lambda attempt, exc, delay: log.warning(
                    "llm_retry", extra={"attempt": attempt, "error": type(exc).__name__}
                ),
                sleep=self._sleep,
            )

        try:
            result = run(base_limit)
            # Reasoning models can spend the whole budget on reasoning_content
            # and return an empty body with finish_reason=length. One retry with
            # a larger budget is cheap compared with silently losing the PR.
            if not result.content.strip() and result.finish_reason == "length":
                retry_limit = min(32768, max(base_limit + 1, base_limit * 2))
                if retry_limit > base_limit:
                    log.warning(
                        "llm_length_retry",
                        extra={"first_limit": base_limit, "retry_limit": retry_limit},
                    )
                    first = result
                    result = run(retry_limit)
                    result.prompt_tokens += first.prompt_tokens
                    result.completion_tokens += first.completion_tokens
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM call failed: {exc}") from exc

        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    def available(self) -> bool:
        return self.settings.llm_available()
