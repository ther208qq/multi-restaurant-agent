"""Tests for the LLM provider seam (``agent/llm.py`` + ``agent/llm_providers.py``).

The point of the seam is that swapping the transport (a local Ollama server vs.
any OpenAI-compatible API keyed with ``OPENAI_API_KEY``) changes nothing for the
nodes. These tests pin the request shape, the response parsing, the offline
fallback, and the fact that no node still reaches for the old client name.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx2

from backend.app.agent import llm_providers
from backend.app.agent.llm import llm_client
from backend.app.agent.llm_providers import (
    OllamaClient,
    OpenAICompatibleClient,
    build_provider,
    rule_based_fallback,
    trust_env_for,
)
from backend.app.config import settings

APP_DIR = Path(__file__).resolve().parents[1] / "app"
NODES_DIR = APP_DIR / "agent" / "nodes"
LLM_NODES = ("intent_router.py", "order_node.py", "reservation_node.py")


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` and records the outgoing request."""

    calls = []
    response = _FakeResponse()
    last_kwargs = {}

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        _FakeAsyncClient.last_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None, **kwargs):
        _FakeAsyncClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeAsyncClient.response


def _install_fake_http(monkeypatch, response):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = response
    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def test_build_provider_selects_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    assert isinstance(build_provider(), OllamaClient)

    for alias in ("openai", "openai-compatible", "api", "OpenAI"):
        monkeypatch.setattr(settings, "LLM_PROVIDER", alias)
        assert isinstance(build_provider(), OpenAICompatibleClient)

    # Unknown values degrade to Ollama rather than crashing the app.
    monkeypatch.setattr(settings, "LLM_PROVIDER", "nonsense")
    assert isinstance(build_provider(), OllamaClient)


def test_llm_client_facade_delegates(monkeypatch):
    """The facade must forward arguments verbatim to whichever provider is set."""
    captured = {}

    class _Recorder:
        async def generate(self, prompt, system_prompt=None, temperature=0.1):
            captured.update(
                prompt=prompt, system_prompt=system_prompt, temperature=temperature
            )
            return "ok"

    monkeypatch.setattr(llm_client, "provider", _Recorder())
    import asyncio

    result = asyncio.run(
        llm_client.generate("hi", system_prompt="sys", temperature=0.7)
    )
    assert result == "ok"
    assert captured == {"prompt": "hi", "system_prompt": "sys", "temperature": 0.7}


def test_nodes_no_longer_reference_the_ollama_client_name():
    """Regression guard: the seam is named ``llm_client`` everywhere."""
    for name in LLM_NODES:
        source = (NODES_DIR / name).read_text(encoding="utf-8")
        assert "ollama_client" not in source, name
        assert "llm_client" in source, name


class _FakeCompletions:
    """Mimics ``client.chat.completions`` from the official openai SDK."""

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeOpenAI:
    """Injected in place of ``AsyncOpenAI`` so tests need no key and no network."""

    def __init__(self, content=None, error=None):
        self.completions = _FakeCompletions(content, error)
        self.chat = SimpleNamespace(completions=self.completions)


def _openai_client(**fake_kwargs):
    fake = _FakeOpenAI(**fake_kwargs)
    client = OpenAICompatibleClient(
        api_key="sk-test-key",
        base_url="https://example.test/v1",
        model="deepseek-chat",
        client=fake,
    )
    return client, fake


async def test_sdk_client_sends_model_messages_and_temperature():
    client, fake = _openai_client(content="hello back")
    result = await client.generate(
        "hello there", system_prompt="you are a bot", temperature=0.3
    )
    assert result == "hello back"
    assert fake.completions.requests == [
        {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "you are a bot"},
                {"role": "user", "content": "hello there"},
            ],
            "temperature": 0.3,
            "stream": False,
        }
    ]


async def test_sdk_client_strips_think_tags():
    client, _ = _openai_client(content='<think>reasoning</think>{"intent": "order"}')
    assert await client.generate("I want pizza") == '{"intent": "order"}'


async def test_sdk_client_falls_back_on_api_error():
    client, _ = _openai_client(error=RuntimeError("401 invalid api key"))
    result = await client.generate(
        "I would like to order a pizza", system_prompt="Classify intent now"
    )
    assert json.loads(result)["intent"] == "order"


async def test_sdk_client_falls_back_when_the_call_is_empty():
    client, _ = _openai_client(content=None)
    assert await client.generate("hi") == rule_based_fallback("hi")


def test_client_without_api_key_never_constructs_a_transport(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("AsyncOpenAI must not be built without a key")

    monkeypatch.setattr(llm_providers, "AsyncOpenAI", _boom)
    client = OpenAICompatibleClient(api_key="", base_url="https://api.example/v1")
    assert client._client is None


async def test_client_without_api_key_still_falls_back():
    client = OpenAICompatibleClient(api_key="", base_url="https://api.example/v1")
    assert await client.generate("hello") == rule_based_fallback("hello")


def test_sdk_wiring_passes_base_url_retries_and_proxy_policy(monkeypatch):
    captured = {}

    def _recorder(**kwargs):
        captured.update(kwargs)
        return _FakeOpenAI()

    monkeypatch.setattr(llm_providers, "AsyncOpenAI", _recorder)

    OpenAICompatibleClient(
        api_key="sk-1", base_url="https://api.deepseek.com/v1", model="deepseek-chat"
    )
    assert captured["api_key"] == "sk-1"
    assert captured["base_url"] == "https://api.deepseek.com/v1"
    assert captured["max_retries"] == 2
    # Cloud endpoints keep the system proxy; loopback endpoints must bypass it.
    assert isinstance(captured["http_client"], httpx2.AsyncClient)
    assert captured["http_client"].trust_env is True

    OpenAICompatibleClient(api_key="sk-1", base_url="http://127.0.0.1:8000/v1")
    assert captured["http_client"].trust_env is False


async def test_ollama_client_still_uses_api_chat(monkeypatch):
    fake = _install_fake_http(
        monkeypatch, _FakeResponse(payload={"message": {"content": "hi from ollama"}})
    )
    client = OllamaClient(base_url="http://localhost:11434", model="deepseek-r1:8b")
    assert await client.generate("hi") == "hi from ollama"
    assert fake.calls[0]["url"] == "http://localhost:11434/api/chat"
    assert fake.calls[0]["headers"] is None


def test_rule_based_fallback_classifies_intents():
    assert json.loads(rule_based_fallback("pizza please", "Classify intent"))["intent"] == "order"
    assert json.loads(rule_based_fallback("book a table", "Classify intent"))["intent"] == "reservation"
    # Precedence is order > reservation > status > faq, so each probe below must
    # avoid the earlier keyword lists.
    assert json.loads(rule_based_fallback("track", "Classify intent"))["intent"] == "status"
    assert json.loads(rule_based_fallback("what are your hours", "Classify intent"))["intent"] == "faq"
    assert json.loads(rule_based_fallback("hi", "Classify intent"))["intent"] == "general"


def test_strip_think_tags_still_importable_from_llm_module():
    """Backwards compatibility for ``from backend.app.agent.llm import ...``."""
    from backend.app.agent.llm import strip_think_tags

    assert strip_think_tags("<think>x</think>answer") == "answer"


def test_loopback_endpoints_bypass_the_system_proxy():
    """A system proxy returns 502 for loopback ports; remote hosts keep it."""
    assert trust_env_for("http://localhost:11434") is False
    assert trust_env_for("http://127.0.0.1:11434") is False
    assert trust_env_for("http://127.0.0.1:8080/v1") is False
    assert trust_env_for("https://api.openai.com/v1") is True
    assert trust_env_for("https://api.deepseek.com/v1") is True
    assert trust_env_for("https://abc-def.ngrok-free.app/v1") is True


async def test_ollama_client_passes_trust_env_through_to_httpx(monkeypatch):
    _install_fake_http(monkeypatch, _FakeResponse())
    await OllamaClient(base_url="http://127.0.0.1:11434").generate("hi")
    assert _FakeAsyncClient.last_kwargs["trust_env"] is False
