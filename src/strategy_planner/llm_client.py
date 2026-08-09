# -*- coding: utf-8 -*-
"""
===================================
LLM 客户端 — 简单 OpenAI 兼容调用
===================================

从项目 Config 读取 LLM 配置，支持 OpenAI 兼容的 API 调用。
"""
import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class LLMClient:
    """简单的 LLM 调用客户端，兼容 OpenAI API 格式"""

    def __init__(self):
        self._model: Optional[str] = None
        self._api_key: Optional[str] = None
        self._base_url: Optional[str] = None
        self._temperature: float = 0.7
        self._max_tokens: int = 8000
        self._initialized = False

    def _ensure_initialized(self):
        if self._initialized:
            return
        from src.config import get_config

        config = get_config()
        model_list = config.llm_model_list if hasattr(config, 'llm_model_list') else []
        self._model = config.litellm_model or "deepseek/deepseek-chat"
        self._temperature = config.llm_temperature if hasattr(config, 'llm_temperature') else 0.7

        # 优先从 model_list 获取
        model_info = None
        for m in model_list:
            mname = m.get("model_name", "") or m.get("model", "")
            if mname == self._model:
                model_info = m
                break
        if not model_info and model_list:
            model_info = model_list[0]
            if model_info:
                self._model = model_info.get("model_name", "") or model_info.get("model", "") or self._model

        if model_info:
            litellm_params = model_info.get("litellm_params", {})
            self._api_key = litellm_params.get("api_key", "")
            self._base_url = litellm_params.get("api_base", "")

        # 回退：直接用 env 中的 DEEPSEEK_API_KEY
        if not self._api_key:
            import os
            self._api_key = os.getenv("DEEPSEEK_API_KEY", "")

        # 确保 base_url 以 /v1 结尾
        if self._base_url and not self._base_url.rstrip("/").endswith("/v1"):
            self._base_url = self._base_url.rstrip("/") + "/v1"

        self._initialized = True
        logger.info(f"LLM 客户端初始化: model={self._model}, base_url={self._base_url}")

    @property
    def available(self) -> bool:
        self._ensure_initialized()
        return bool(self._api_key)

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        """发送聊天请求，返回回复文本"""
        self._ensure_initialized()

        if not self.available:
            return "[错误] LLM 未配置，请设置 DEEPSEEK_API_KEY 或 LLM_CHANNELS"

        url = f"{self._base_url}/chat/completions" if self._base_url else ""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model.replace("deepseek/", "") if self._model.startswith("deepseek/") else self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": self._max_tokens,
        }

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
                else:
                    error_text = resp.text[:500]
                    logger.warning(f"LLM 请求失败 (attempt {attempt + 1}): status={resp.status_code}, body={error_text}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"LLM 请求异常 (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        return "[错误] LLM 请求失败，已重试3次"


# 全局单例
_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
