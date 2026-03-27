# -*- coding: utf-8 -*-
"""
Simple LLM Client - Direct API calls without LiteLLM.

Supports Gemini and DeepSeek with simple fallback logic.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Normalized response from LLM."""
    content: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    error: Optional[str] = None


class LLMClient:
    """Simple LLM client with direct API calls."""

    def __init__(self):
        self.config = get_config()
        self._gemini_key = self._get_first_key(self.config.gemini_api_keys)
        self._deepseek_key = self._get_first_key(self.config.deepseek_api_keys)
        self._gemini_quota_exhausted = False  # 标记 Gemini 额度是否已耗尽

    def _get_first_key(self, keys: List[str]) -> Optional[str]:
        """Get first valid API key."""
        for key in keys:
            if key and len(key) >= 8:
                return key
        return None

    def is_available(self) -> bool:
        """Check if any LLM is configured."""
        return bool(self._gemini_key or self._deepseek_key)

    def call(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 8192,
        timeout: float = 60.0,
    ) -> LLMResponse:
        """
        Call LLM with fallback.

        Priority: Gemini -> DeepSeek
        Once Gemini returns 429, it will be skipped for all subsequent calls.
        """
        # Try Gemini first (only if not quota exhausted)
        if self._gemini_key and not self._gemini_quota_exhausted:
            try:
                return self._call_gemini(messages, temperature, max_tokens, timeout)
            except Exception as e:
                error_str = str(e)
                logger.warning(f"Gemini failed: {e}")
                # Mark as exhausted on 429 (quota exceeded)
                if "429" in error_str or "quota" in error_str.lower() or "exhausted" in error_str.lower():
                    self._gemini_quota_exhausted = True
                    logger.info("Gemini quota exhausted, will use DeepSeek for remaining calls")
                else:
                    # For other errors, wait a bit then try fallback
                    time.sleep(2)

        # Fallback to DeepSeek
        if self._deepseek_key:
            try:
                return self._call_deepseek(messages, temperature, max_tokens, timeout)
            except Exception as e:
                logger.warning(f"DeepSeek failed: {e}")
                return LLMResponse(error=str(e), provider="deepseek")

        return LLMResponse(error="No LLM available", provider="none")

    def _call_gemini(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> LLMResponse:
        """Call Gemini API directly."""
        model = "gemini-2.0-flash"  # Default model

        # Convert messages to Gemini format
        contents = []
        system_instruction = None

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = content
                continue

            gemini_role = "model" if role == "assistant" else "user"
            contents.append({
                "role": gemini_role,
                "parts": [{"text": content}]
            })

        # Build request
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        params = {"key": self._gemini_key}

        body: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }

        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        # Make request
        response = requests.post(
            url,
            params=params,
            json=body,
            timeout=timeout,
        )

        if response.status_code == 429:
            raise Exception("429 Quota exceeded")
        if response.status_code == 503:
            raise Exception("503 Service unavailable")
        response.raise_for_status()

        data = response.json()

        # Extract content
        candidates = data.get("candidates", [])
        if not candidates:
            raise Exception("No candidates in response")

        content_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")

        # Extract usage
        usage_metadata = data.get("usageMetadata", {})
        usage = {
            "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
            "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
            "total_tokens": usage_metadata.get("totalTokenCount", 0),
        }

        return LLMResponse(
            content=content_text,
            usage=usage,
            provider="gemini",
            model=f"gemini/{model}",
        )

    def _call_deepseek(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> LLMResponse:
        """Call DeepSeek API directly."""
        model = "deepseek-chat"

        headers = {
            "Authorization": f"Bearer {self._deepseek_key}",
            "Content-Type": "application/json",
        }

        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # 使用配置中的 base_url，默认为官方 API
        base_url = getattr(self.config, 'deepseek_base_url', 'https://api.deepseek.com/v1').rstrip('/')
        url = f"{base_url}/chat/completions"

        response = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=timeout,
        )

        response.raise_for_status()
        data = response.json()

        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")

        usage_data = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_data.get("prompt_tokens", 0),
            "completion_tokens": usage_data.get("completion_tokens", 0),
            "total_tokens": usage_data.get("total_tokens", 0),
        }

        return LLMResponse(
            content=content,
            usage=usage,
            provider="deepseek",
            model=f"deepseek/{model}",
        )

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 8192,
        timeout: float = 60.0,
    ) -> Tuple[Optional[str], Optional[List[Dict]], str, str]:
        """
        Call LLM with tool declarations.

        Returns: (content, tool_calls, provider, model)
        """
        # Simplified: convert tools to system prompt for now
        # Full tool calling would require provider-specific implementations
        tools_prompt = "\n\n可用工具:\n" + json.dumps(tools, ensure_ascii=False, indent=2)

        # Add tools to last user message
        modified_messages = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and i == len(messages) - 1:
                modified_messages.append({
                    "role": "user",
                    "content": msg.get("content", "") + tools_prompt
                })
            else:
                modified_messages.append(msg)

        response = self.call(modified_messages, temperature, max_tokens, timeout)

        if response.error:
            return None, None, response.provider, response.model

        # Try to parse tool calls from response
        tool_calls = None
        content = response.content or ""

        # Look for JSON tool calls in response
        if "tool_call" in content.lower() or "function" in content.lower():
            try:
                # Try to extract JSON
                start = content.find("[")
                end = content.rfind("]")
                if start >= 0 and end > start:
                    tool_calls = json.loads(content[start:end+1])
            except json.JSONDecodeError:
                pass

        return content, tool_calls, response.provider, response.model


# Global client instance
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get or create global LLM client."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
