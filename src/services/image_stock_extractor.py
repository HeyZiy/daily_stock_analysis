# -*- coding: utf-8 -*-
"""
===================================
图片股票代码提取 (Vision LLM)
===================================

从截图/图片中提取股票代码，使用 Vision LLM。
优先级：Gemini -> DeepSeek（首个可用）。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from typing import List, Optional, Tuple

import requests

from src.config import Config, get_config

logger = logging.getLogger(__name__)


EXTRACT_PROMPT = """请分析这张股票市场截图或图片，提取其中所有可见的股票代码及名称。

重要：若图中同时显示股票名称和代码（如自选股列表、ETF 列表），必须同时提取两者，每个元素必须包含 code 和 name 字段。

输出格式：仅返回有效的 JSON 数组，不要 markdown、不要解释。
每个元素为对象：{"code":"股票代码","name":"股票名称","confidence":"high|medium|low"}
- code: 必填，股票代码（A股6位、港股5位、美股1-5字母、ETF 如 159887/512880）
- name: 若图中有名称则必填（如 贵州茅台、银行ETF、证券ETF），与代码一一对应；仅当图中确实无名称时可省略
- confidence: 必填，识别置信度，high=确定、medium=较确定、low=不确定

示例（图中同时有名称和代码时）：
- 个股：600519 贵州茅台、300750 宁德时代
- 港股：00700 腾讯控股、09988 阿里巴巴
- 美股：AAPL 苹果、TSLA 特斯拉
- ETF：159887 银行ETF、512880 证券ETF、512000 券商ETF、512480 半导体ETF、515030 新能源车ETF

输出示例：[{"code":"600519","name":"贵州茅台","confidence":"high"},{"code":"159887","name":"银行ETF","confidence":"high"}]

禁止只返回代码数组如 ["159887","512880"]，必须使用对象格式。若未找到任何股票代码，返回：[]"""

# Valid confidence values; invalid ones normalized to medium
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

# LLM sometimes returns JSON field names or markdown labels as "code"; filter these out
_FAKE_CODES = frozenset({"CODE", "NAME", "HIGH", "LOW", "MEDIUM", "CONFIDENCE", "JSON"})

ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5MB
VISION_API_TIMEOUT = 60  # seconds

# Magic bytes for server-side MIME validation
_IMAGE_SIGNATURES = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],
}


def _verify_image_magic_bytes(image_bytes: bytes, mime_type: str) -> None:
    """Verify actual file content matches declared MIME type."""
    if len(image_bytes) < 12:
        raise ValueError("图片文件过小或损坏")
    if mime_type not in _IMAGE_SIGNATURES:
        raise ValueError(f"无法验证类型: {mime_type}")
    if mime_type == "image/webp":
        if image_bytes[:4] != b"RIFF" or image_bytes[8:12] != b"WEBP":
            raise ValueError("文件内容与声明的类型 image/webp 不匹配")
        return
    for sig in _IMAGE_SIGNATURES[mime_type]:
        if image_bytes.startswith(sig):
            return
    raise ValueError(f"文件内容与声明的类型 {mime_type} 不匹配")


def _normalize_code(raw: str) -> Optional[str]:
    """Normalize and validate a single stock code."""
    s = raw.strip().upper()
    if not s:
        return None
    # A-shares & HK: 5-6 digit codes
    if s.isdigit() and len(s) in (5, 6):
        return s
    # US stocks: 1-5 letters
    if re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", s):
        return s
    # Try to remove SH/SZ suffix
    for suffix in (".SH", ".SZ", ".SS"):
        if s.endswith(suffix):
            base = s[: -len(suffix)].strip()
            if base.isdigit() and len(base) in (5, 6):
                return base
    return None


def _parse_codes_from_text(text: str) -> List[str]:
    """Extract stock codes from LLM response text (legacy format)."""
    seen: set[str] = set()
    result: List[str] = []

    cleaned = text.strip()
    for start in ("```json", "```"):
        if cleaned.startswith(start):
            cleaned = cleaned[len(start):].strip()
            break
    end_idx = cleaned.rfind("```")
    if end_idx >= 0:
        cleaned = cleaned[:end_idx].strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    c = _normalize_code(item)
                    if c and c not in seen and c not in _FAKE_CODES:
                        seen.add(c)
                        result.append(c)
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: find 5-6 digit numbers and US stock codes
    for m in re.finditer(r"\b([0-9]{5,6}|[A-Z]{1,5}(\.[A-Z])?)\b", text, re.IGNORECASE):
        c = _normalize_code(m.group(1))
        if c and c not in seen and c not in _FAKE_CODES:
            seen.add(c)
            result.append(c)

    return result


def _parse_items_from_text(text: str) -> List[Tuple[str, Optional[str], str]]:
    """Parse LLM response into items (code, name, confidence)."""
    cleaned = text.strip()
    for start in ("```json", "```"):
        if cleaned.startswith(start):
            cleaned = cleaned[len(start):].strip()
            break
    end_idx = cleaned.rfind("```")
    if end_idx >= 0:
        cleaned = cleaned[:end_idx].strip()

    parsed_data = None
    try:
        parsed_data = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            parsed_data = repair_json(cleaned, return_objects=True)
            logger.debug("[ImageExtractor] json.loads failed, repaired malformed JSON")
        except Exception:
            parsed_data = None

    if isinstance(parsed_data, list):
        seen: set[str] = set()
        result: List[Tuple[str, Optional[str], str]] = []
        for item in parsed_data:
            if not isinstance(item, dict):
                continue
            code_raw = item.get("code") if isinstance(item.get("code"), str) else None
            if not code_raw:
                continue
            code = _normalize_code(code_raw)
            if not code or code in seen or code in _FAKE_CODES:
                continue
            seen.add(code)
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                name = name.strip()
            else:
                name = None
            conf = item.get("confidence")
            if isinstance(conf, str) and conf.lower() in _VALID_CONFIDENCE:
                conf = conf.lower()
            else:
                conf = "medium"
            result.append((code, name, conf))
        if result:
            return result

    # Fallback: legacy format (codes only)
    codes = _parse_codes_from_text(text)
    if not codes:
        logger.info("[ImageExtractor] 无法解析为结构化 items")
    return [(c, None, "medium") for c in codes]


def _get_first_key(keys: List[str]) -> Optional[str]:
    """Get first valid API key."""
    for key in keys:
        if key and len(key) >= 8:
            return key
    return None


def _call_gemini_vision(image_b64: str, mime_type: str, api_key: str) -> str:
    """Call Gemini Vision API directly."""
    model = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": api_key}

    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": EXTRACT_PROMPT},
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": image_b64
                    }
                }
            ]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
        }
    }

    response = requests.post(
        url,
        params=params,
        json=body,
        timeout=VISION_API_TIMEOUT,
    )

    response.raise_for_status()
    data = response.json()

    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("No candidates in Gemini vision response")

    content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    if not content:
        raise ValueError("Gemini vision returned empty content")

    return content


def _call_deepseek_vision(image_b64: str, mime_type: str, api_key: str) -> str:
    """Call DeepSeek Vision API (if supported)."""
    # DeepSeek currently doesn't support vision, but we keep this for future
    raise ValueError("DeepSeek does not support vision API yet")


def extract_stock_codes_from_image(
    image_bytes: bytes,
    mime_type: str,
) -> Tuple[List[Tuple[str, Optional[str], str]], str]:
    """
    从图片中提取股票代码及名称。

    优先级：Gemini -> 失败返回错误。
    支持重试（最多 3 次，指数退避）。

    Args:
        image_bytes: 原始图片字节
        mime_type: MIME 类型

    Returns:
        (items, raw_text) - items 为 [(code, name?, confidence), ...]
    """
    mime_type = (mime_type or "image/jpeg").strip().lower().split(";")[0].strip()
    if mime_type not in ALLOWED_MIME:
        raise ValueError(f"不支持的图片类型: {mime_type}")

    if not image_bytes:
        raise ValueError("图片内容为空")

    if len(image_bytes) > MAX_SIZE_BYTES:
        raise ValueError(f"Image too large (max {MAX_SIZE_BYTES // (1024 * 1024)}MB)")

    _verify_image_magic_bytes(image_bytes, mime_type)

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    cfg = get_config()

    # Get API keys
    gemini_key = _get_first_key(cfg.gemini_api_keys)

    if not gemini_key:
        raise ValueError("未配置 Gemini API Key，无法使用图片识别功能")

    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            raw = _call_gemini_vision(image_b64, mime_type, gemini_key)
            logger.debug("[ImageExtractor] raw LLM response:\n%s", raw)
            items = _parse_items_from_text(raw)
            logger.info(
                f"[ImageExtractor] Gemini 提取 {len(items)} 个: "
                f"{[(i[0], i[1]) for i in items[:5]]}{'...' if len(items) > 5 else ''}"
            )
            return items, raw
        except Exception as e:
            last_error = e
            if attempt < 2:
                delay = 2 ** attempt
                logger.warning(f"[ImageExtractor] 尝试 {attempt + 1}/3 失败，{delay}s 后重试: {e}")
                time.sleep(delay)

    raise ValueError(f"图片识别失败 (3次尝试): {last_error}")
