"""Tests for the LLM client, response parser, prompts, generator, and swarm."""

from __future__ import annotations

import threading

import pytest

from loki.config import LLMConfig
from loki.errors import LLMError, ParseError
from loki.generate.contextpack import build_context_pack
from loki.generate.generator import generate
from loki.generate.worker_pool import RateLimiter, run_swarm
from loki.llm.client import LLMClient
from loki.llm.parse import parse_generation_response
from loki.state.model import Collaborator, Task, TaskState
from loki.state.store import StateStore

VALID_RESPONSE = """\
PLAN:
- happy path: add returns the sum
- boundary: adding zero

```java
package com.acme;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.assertThat;
class CalculatorTest {
    @Test
    void addsTwoNumbers() {
        assertThat(new Calculator().add(2, 3)).isEqualTo(5);
    }
}
```
"""


def make_task() -> Task:
    return Task(
        id="t1",
        fqcn="com.acme.Calculator",
        module="app",
        source_path="src/main/java/com/acme/Calculator.java",
        test_path="src/test/java/com/acme/CalculatorTest.java",
        collaborators=[Collaborator("com.acme.Ledger", True, ["int total()"])],
    )


# --- parser ---------------------------------------------------------------

def test_parse_valid_response() -> None:
    result = parse_generation_response(VALID_RESPONSE)
    assert "class CalculatorTest" in result.test_source
    assert result.plan == ["happy path: add returns the sum", "boundary: adding zero"]


def test_parse_rejects_no_code_block() -> None:
    with pytest.raises(ParseError):
        parse_generation_response("PLAN:\n- do stuff\nNo code here.")


def test_parse_rejects_multiple_classes() -> None:
    raw = "```java\nclass A {}\n```\n```java\nclass B {}\n```"
    with pytest.raises(ParseError):
        parse_generation_response(raw)


def test_parse_rejects_empty() -> None:
    with pytest.raises(ParseError):
        parse_generation_response("   ")


# --- client ---------------------------------------------------------------

class FakeTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post_json(self, url, headers, body, timeout) -> dict:
        self.calls.append({"url": url, "headers": headers, "body": body})
        return self.response


def llm_config() -> LLMConfig:
    return LLMConfig(base_url="https://vllm/v1", model="minimax", api_key_env="TOK")


def test_client_extracts_content(monkeypatch) -> None:
    monkeypatch.setenv("TOK", "secret")
    transport = FakeTransport({"choices": [{"message": {"content": "hello"}}]})
    client = LLMClient(llm_config(), transport)
    assert client.complete("sys", "user") == "hello"
    call = transport.calls[0]
    assert call["url"] == "https://vllm/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["body"]["model"] == "minimax"


def test_client_raises_on_malformed_response(monkeypatch) -> None:
    monkeypatch.setenv("TOK", "secret")
    client = LLMClient(llm_config(), FakeTransport({"nope": True}))
    with pytest.raises(LLMError):
        client.complete("sys", "user")


# --- generator (with reformat retry) --------------------------------------

class ScriptedClient:
    """Returns queued responses; records prompts."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, temperature=None) -> str:
        self.prompts.append(user)
        return self._responses.pop(0)


def test_generate_happy_path() -> None:
    client = ScriptedClient([VALID_RESPONSE])
    pack = build_context_pack(make_task(), "class Calculator {}", None, {}, [], 90_000)
    result = generate(client, make_task(), pack)
    assert "CalculatorTest" in result.test_source


def test_generate_retries_once_on_bad_format() -> None:
    client = ScriptedClient(["garbage, no code", VALID_RESPONSE])
    pack = build_context_pack(make_task(), "class Calculator {}", None, {}, [], 90_000)
    result = generate(client, make_task(), pack)
    assert "CalculatorTest" in result.test_source
    assert len(client.prompts) == 2
    assert "required format" in client.prompts[1]


def test_generate_raises_after_retry_fails() -> None:
    client = ScriptedClient(["garbage", "still garbage"])
    pack = build_context_pack(make_task(), "class Calculator {}", None, {}, [], 90_000)
    with pytest.raises(ParseError):
        generate(client, make_task(), pack)


# --- context pack ---------------------------------------------------------

def test_context_pack_drops_exemplar_when_over_budget() -> None:
    big_exemplar = "x" * 10_000
    pack = build_context_pack(make_task(), "class C {}", big_exemplar, {}, [], max_context_tokens=100)
    assert pack.exemplar_test is None  # trimmed to fit


def test_context_pack_keeps_collaborators() -> None:
    pack = build_context_pack(make_task(), "class C {}", None, {"docker": True}, [], 90_000)
    joined = "\n".join(pack.collaborator_signatures)
    assert "com.acme.Ledger" in joined
    assert "int total()" in joined


# --- swarm ----------------------------------------------------------------

def test_run_swarm_processes_all_tasks_once(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store._tasks = {
        f"t{i}": Task(
            id=f"t{i}", fqcn=f"com.acme.C{i}", module="app",
            source_path="s", test_path="t",
        )
        for i in range(50)
    }
    handled: list[str] = []
    lock = threading.Lock()

    def handler(task: Task) -> None:
        with lock:
            handled.append(task.id)
        task.state = TaskState.PASSED
        store.update(task)

    n = run_swarm(store, handler, pool_size=8)
    assert n == 50
    assert sorted(handled) == sorted(f"t{i}" for i in range(50))
    assert len(store.tasks_by_state(TaskState.PASSED)) == 50


def test_rate_limiter_spaces_requests() -> None:
    import time

    limiter = RateLimiter(rps=50)  # 20ms apart
    start = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    assert time.monotonic() - start >= 0.02  # at least one interval enforced
