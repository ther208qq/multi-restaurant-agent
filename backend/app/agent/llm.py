"""The agent's single LLM seam.

Nodes and tools call ``llm_client.generate(prompt, system_prompt, temperature)``
and get a string back; the transport (a local Ollama server vs. any
OpenAI-compatible API) is chosen by ``settings.LLM_PROVIDER`` in
:mod:`backend.app.agent.llm_providers`. Swapping providers never touches a node.

``strip_think_tags`` is re-exported here for backwards compatibility.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from backend.app.agent.llm_providers import build_provider, strip_think_tags

logger = logging.getLogger("deepseek_llm")

__all__ = [
    "LLMClient",
    "extract_json_from_text",
    "llm_client",
    "strip_think_tags",
]


def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Defensively extract JSON object from potentially conversational LLM output."""
    cleaned = strip_think_tags(text).strip()
    if not cleaned:
        return None

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Look for ```json ... ``` blocks
    json_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if json_block:
        try:
            return json.loads(json_block.group(1))
        except json.JSONDecodeError:
            pass

    # Look for outermost {...}
    match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return None


class LLMClient:
    """Facade over the provider chosen by ``settings.LLM_PROVIDER``.

    Keeps the historical ``generate(prompt, system_prompt, temperature)``
    signature so nodes and tests stay provider-agnostic.
    """

    def __init__(self, provider=None):
        self.provider = provider or build_provider()

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
    ) -> str:
        return await self.provider.generate(
            prompt, system_prompt=system_prompt, temperature=temperature
        )


llm_client = LLMClient()
