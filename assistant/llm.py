"""LLM adapters.

Providers (selected with LLM_PROVIDER, see from_env):
  openrouter  OpenAI-compatible chat endpoint at openrouter.ai. Default when
              OPENROUTER_API_KEY is set. Uses the standard library only.
  anthropic   Anthropic Messages API via the anthropic SDK. Needs ANTHROPIC_API_KEY.
  mock        Deterministic, offline. Used by tests and when no key is set.

Keys are read from the environment only. They are never written to the repo,
logged, or returned in error messages. Model IDs come from the environment with
defaults:
  OPENROUTER_MODEL  anthropic/claude-sonnet-5.5   (verified against the OpenRouter model list)
  ANTHROPIC_MODEL   claude-sonnet-5-5
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol


class LLMError(RuntimeError):
    """Raised for provider failures. The message never contains the API key."""


class LLMClient(Protocol):
    def generate(self, prompt: str, *, max_tokens: int, timeout_s: float) -> str: ...


class MockLLM:
    """Deterministic stand-in for tests and offline runs."""

    def generate(self, prompt: str, *, max_tokens: int, timeout_s: float) -> str:
        return "[mock] " + prompt[: max(0, min(max_tokens, 200))]


class OpenRouterLLM:
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        endpoint: str | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("OpenRouter API key is empty")
        self._api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self._endpoint = endpoint or self.ENDPOINT
        self._open = opener or urllib.request.urlopen

    def generate(self, prompt: str, *, max_tokens: int, timeout_s: float) -> str:
        body = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Status only. Never echo the request, which carries the key.
            raise LLMError(f"OpenRouter returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"OpenRouter request failed: {type(exc).__name__}") from None
        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("OpenRouter response had no message content") from None


class AnthropicLLM:
    DEFAULT_MODEL = "claude-sonnet-5-5"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        import anthropic  # imported lazily so tests run without the SDK

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", self.DEFAULT_MODEL)
        self._client = anthropic.Anthropic(api_key=key)

    def generate(self, prompt: str, *, max_tokens: int, timeout_s: float) -> str:
        msg = self._client.with_options(timeout=timeout_s).messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def from_env(env: Mapping[str, str] | None = None) -> LLMClient:
    """Build the client named by LLM_PROVIDER, falling back to the first configured key."""
    env = os.environ if env is None else env
    provider = env.get("LLM_PROVIDER", "").lower()
    if not provider:
        if env.get("OPENROUTER_API_KEY"):
            provider = "openrouter"
        elif env.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        else:
            provider = "mock"
    if provider == "openrouter":
        return OpenRouterLLM(env.get("OPENROUTER_API_KEY", ""), env.get("OPENROUTER_MODEL"))
    if provider == "anthropic":
        return AnthropicLLM(env.get("ANTHROPIC_MODEL"), env.get("ANTHROPIC_API_KEY"))
    if provider == "mock":
        return MockLLM()
    raise LLMError(f"unknown LLM_PROVIDER '{provider}'")
