"""LLM transport providers behind a single ``generate()`` seam.

Agent nodes never talk to a provider directly: they call
``backend.app.agent.llm.llm_client.generate(...)``, which dispatches to one of
the clients below based on ``settings.LLM_PROVIDER``:

* ``"ollama"`` - a local or tunnelled Ollama server (``POST /api/chat``).
* ``"openai"`` - any OpenAI-compatible endpoint, driven by the official
  ``openai`` SDK (``AsyncOpenAI``): OpenAI, DeepSeek, Qwen/DashScope, Moonshot,
  Groq, OpenRouter, vLLM, LM Studio, ... Point ``OPENAI_BASE_URL`` at the
  provider's ``/v1`` root and set ``OPENAI_API_KEY`` / ``OPENAI_MODEL``.

Every client degrades to :func:`rule_based_fallback` when the endpoint is
unreachable, so the graph keeps working offline.
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
import httpx2
from openai import AsyncOpenAI

from backend.app.config import settings

logger = logging.getLogger("deepseek_llm")

LLM_TIMEOUT_SECONDS = 45.0
OPENAI_PROVIDER_ALIASES = {"openai", "openai-compatible", "api"}
LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def trust_env_for(base_url: str) -> bool:
    """Whether httpx should honour the system HTTP proxy for this endpoint.

    A system proxy (Clash and friends on Windows/macOS) answers 502 for loopback
    ports, so a local Ollama / vLLM / LM Studio server is unreachable unless the
    proxy is bypassed. Remote endpoints -- including an Ollama tunnel -- keep
    honouring the proxy, which they may well need.
    """
    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname not in LOOPBACK_HOSTNAMES


def strip_think_tags(text: str) -> str:
    """Strip DeepSeek R1 <think>...</think> reasoning tags and contents."""
    if not text:
        return ""
    # Remove complete <think>...</think> blocks (across multiple lines)
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove unclosed <think> tag if text got truncated mid-reasoning
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL)
    # Remove orphaned closing tags
    cleaned = cleaned.replace("</think>", "").strip()
    return cleaned


def rule_based_fallback(prompt: str, system_prompt: Optional[str] = None) -> str:
    """Deterministic offline reply used by every provider on failure.

    Keeps the agent usable when no LLM endpoint is configured or reachable.
    """
    lower_prompt = prompt.lower()
    if "classify intent" in (system_prompt or "").lower():
        if any(k in lower_prompt for k in ["order", "pizza", "pasta", "drink", "food", "buy", "cart", "eat"]):
            return json.dumps({"intent": "order", "confidence": 0.95})
        if any(k in lower_prompt for k in ["reserve", "table", "book", "seat", "reservation"]):
            return json.dumps({"intent": "reservation", "confidence": 0.95})
        if any(k in lower_prompt for k in ["status", "where is", "track", "my order", "my reservation"]):
            return json.dumps({"intent": "status", "confidence": 0.95})
        if any(k in lower_prompt for k in ["hour", "time", "address", "location", "menu", "open", "phone"]):
            return json.dumps({"intent": "faq", "confidence": 0.95})
        return json.dumps({"intent": "general", "confidence": 0.8})

    return "Welcome to our restaurant! I can help you browse the menu, place an order, book a table, or check the status of your existing orders and reservations. How may I assist you today?"


class OllamaClient:
    """Async HTTP client for a local or tunnelled Ollama server."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
    ) -> str:
        """Call Ollama /api/chat with timeout and graceful error fallback."""
        url = f"{self.base_url}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }

        try:
            async with httpx.AsyncClient(
                timeout=LLM_TIMEOUT_SECONDS, trust_env=trust_env_for(self.base_url)
            ) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    data = response.json()
                    content = data.get("message", {}).get("content", "")
                    return strip_think_tags(content)
                logger.warning(
                    "Ollama returned HTTP %d: %s. Using fallback parser.",
                    response.status_code,
                    response.text,
                )
        except Exception as e:
            logger.warning(
                "Could not reach Ollama endpoint at %s (%s). Using defensive agent fallback.",
                self.base_url,
                str(e),
            )

        # Graceful fallback when the Ollama server/tunnel is disconnected or offline
        return rule_based_fallback(prompt, system_prompt)


class OpenAICompatibleClient:
    """Client for any OpenAI-compatible endpoint, built on the official SDK.

    ``AsyncOpenAI`` is the mainstream transport for these APIs, so retries with
    backoff, timeouts and typed exceptions come for free. Swapping ``base_url``
    covers OpenAI, DeepSeek, Qwen/DashScope, Moonshot, Groq, OpenRouter, vLLM and
    LM Studio alike.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        client=None,
    ):
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.base_url = (base_url or settings.OPENAI_BASE_URL).rstrip("/")
        self.model = model or settings.OPENAI_MODEL
        # ``client`` is injectable so tests exercise this with no key and no network.
        self._client = client
        if self._client is None and self.api_key:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=2,
                # httpx2 -- the SDK's own transport -- honours the system proxy by
                # default; loopback endpoints must bypass it, cloud ones must not.
                http_client=httpx2.AsyncClient(
                    timeout=LLM_TIMEOUT_SECONDS,
                    trust_env=trust_env_for(self.base_url),
                ),
            )
        elif self._client is None:
            logger.warning(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is empty; every call will "
                "fall back to the rule-based parser."
            )

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
    ) -> str:
        """Call the endpoint through the SDK, falling back on any failure."""
        if self._client is None:
            return rule_based_fallback(prompt, system_prompt)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=False,
            )
            choices = response.choices or []
            content = choices[0].message.content if choices else ""
            cleaned = strip_think_tags(content or "")
            if cleaned:
                return cleaned
            logger.warning("LLM endpoint returned an empty completion; using fallback parser.")
        except Exception as e:
            logger.warning(
                "LLM endpoint %s failed (%s). Using defensive agent fallback.",
                self.base_url,
                str(e),
            )

        return rule_based_fallback(prompt, system_prompt)


def build_provider():
    """Instantiate the provider selected by ``settings.LLM_PROVIDER``."""
    provider = (settings.LLM_PROVIDER or "ollama").strip().lower()
    if provider in OPENAI_PROVIDER_ALIASES:
        return OpenAICompatibleClient()
    if provider != "ollama":
        logger.warning("Unknown LLM_PROVIDER %r; falling back to 'ollama'.", provider)
    return OllamaClient()


__all__ = [
    "OpenAICompatibleClient",
    "OllamaClient",
    "build_provider",
    "rule_based_fallback",
    "strip_think_tags",
    "trust_env_for",
]
