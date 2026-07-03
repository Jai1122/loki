"""OpenAI-compatible vLLM client (DESIGN.md §2, §14).

The client targets the ``/chat/completions`` endpoint of a self-hosted vLLM
server and authenticates with a bearer token read from config. HTTP is abstracted
behind a small :class:`Transport` protocol so the client is fully testable
without a live endpoint and can use either ``httpx`` (if installed) or the stdlib
``urllib`` — no hard third-party dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

from loki.config import LLMConfig
from loki.errors import LLMError


class Transport(Protocol):
    """Performs one HTTP POST and returns the decoded JSON response body."""

    def post_json(self, url: str, headers: dict[str, str], body: dict, timeout: float) -> dict:
        ...


class UrllibTransport:
    """Default transport using only the Python standard library."""

    def post_json(self, url: str, headers: dict[str, str], body: dict, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"vLLM endpoint returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"vLLM endpoint unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"vLLM endpoint returned non-JSON response: {exc}") from exc


def _default_transport() -> Transport:
    return UrllibTransport()


class LLMClient:
    """Thin wrapper over the chat-completions API."""

    def __init__(self, config: LLMConfig, transport: Transport | None = None) -> None:
        self._config = config
        self._transport = transport or _default_transport()

    def complete(self, system: str, user: str, temperature: float | None = None) -> str:
        """Return the assistant message content for a system+user turn."""
        body = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._config.temperature if temperature is None else temperature,
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._config.base_url}/chat/completions"
        response = self._transport.post_json(url, headers, body, self._config.request_timeout_s)
        return _extract_content(response)


def _extract_content(response: dict) -> str:
    try:
        choices = response["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Malformed chat-completions response: {response!r}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError("Model returned empty content")
    return content
