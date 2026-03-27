# -*- coding: utf-8 -*-
"""
Multi-provider LLM Tool-Calling Adapter.

Simplified version without LiteLLM - uses direct API calls.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.llm_client import LLMClient, get_llm_client
from src.config import get_config

logger = logging.getLogger(__name__)


# ============================================================
# Unified response types
# ============================================================

@dataclass
class ToolCall:
    """A single tool call requested by the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any]
    thought_signature: Optional[str] = None


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    content: Optional[str] = None          # text response (final answer)
    tool_calls: List[ToolCall] = field(default_factory=list)  # tool calls to execute
    reasoning_content: Optional[str] = None  # Chain-of-thought (CoT) from DeepSeek thinking mode
    usage: Dict[str, Any] = field(default_factory=dict)       # token usage info
    provider: str = ""                     # which provider handled this call
    model: str = ""                        # full model name used
    raw: Any = None                        # raw provider response for debugging


# ============================================================
# LLM Tool Adapter
# ============================================================

class LLMToolAdapter:
    """Unified adapter for tool-calling via direct API calls.

    Supports Gemini and DeepSeek with simple fallback logic.
    """

    def __init__(self, config=None):
        config = config or get_config()
        self._config = config
        self._client: Optional[LLMClient] = None
        self._init_client()

    def _init_client(self) -> None:
        """Initialize LLM client."""
        self._client = get_llm_client()
        if not self._client.is_available():
            logger.warning("Agent LLM: No API keys configured")

    @property
    def is_available(self) -> bool:
        """True if LLM is configured and at least one API key is present."""
        return self._client is not None and self._client.is_available()

    @property
    def primary_provider(self) -> str:
        """Provider name from config."""
        model = self._config.litellm_model or ""
        if "/" in model:
            return model.split("/")[0]
        return model or "none"

    # ============================================================
    # Unified call
    # ============================================================

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[dict],
        provider: Optional[str] = None,
    ) -> LLMResponse:
        """Send messages + tool declarations to LLM, return normalized response.

        Args:
            messages: Conversation history in provider-neutral format
            tools: Tool declarations (simplified, converted to prompt)
            provider: Ignored (kept for backward compatibility)

        Returns:
            LLMResponse with either content (final answer) or tool_calls.
        """
        return self.call_completion(messages, tools=tools, provider=provider)

    def call_text(
        self,
        messages: List[Dict[str, Any]],
        *,
        provider: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Send a text-only completion through the shared routing stack."""
        return self.call_completion(
            messages,
            tools=None,
            provider=provider,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def call_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[dict]] = None,
        provider: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Shared completion path for both tool and text-only calls."""
        temp = temperature if temperature is not None else self._config.llm_temperature
        max_tok = max_tokens if max_tokens is not None else 8192
        to = timeout if timeout is not None else 60.0

        # Convert messages to simple format
        simple_messages = []
        for msg in messages:
            simple_msg = {
                "role": msg.get("role", "user"),
                "content": msg.get("content", "") if isinstance(msg.get("content"), str) else json.dumps(msg.get("content", {})),
            }
            simple_messages.append(simple_msg)

        # Call LLM
        response = self._client.call(
            messages=simple_messages,
            temperature=temp,
            max_tokens=max_tok,
            timeout=to,
        )

        if response.error:
            logger.error(f"LLM call failed: {response.error}")
            return LLMResponse(
                content=f"Error: {response.error}",
                provider=response.provider,
                model=response.model,
            )

        # Parse tool calls if present
        tool_calls: List[ToolCall] = []
        content = response.content or ""

        if tools:
            # Try to extract tool calls from content
            tool_calls = self._parse_tool_calls(content)

        return LLMResponse(
            content=content if not tool_calls else None,
            tool_calls=tool_calls,
            usage=response.usage,
            provider=response.provider,
            model=response.model,
        )

    def _parse_tool_calls(self, content: str) -> List[ToolCall]:
        """Parse tool calls from LLM response content."""
        tool_calls: List[ToolCall] = []

        # Look for JSON array in content
        try:
            # Try to find JSON array
            start = content.find("[")
            end = content.rfind("]")
            if start >= 0 and end > start:
                data = json.loads(content[start:end+1])
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "name" in item:
                            tool_calls.append(ToolCall(
                                id=str(uuid.uuid4())[:8],
                                name=item.get("name", ""),
                                arguments=item.get("arguments", item.get("args", {})),
                            ))
        except json.JSONDecodeError:
            pass

        return tool_calls
