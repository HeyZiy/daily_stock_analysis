# -*- coding: utf-8 -*-
"""
===================================
实现检查器 — Agent 2
===================================

将 Agent 1 的候选策略与策略池注册表（data/strategy_registry.json，权威对照表）
做 LLM 语义对账，代码侧只负责校验与簿记：

1. 匹配到池内已实现策略（existing_implementation 非空）→ ✅ 已有实现
2. 匹配到池内未实现策略 → 📋 登记待办（挂到池内条目，按 registry_id 去重）
3. 池内无对应条目 → 📋 登记待办（全新策略）
4. LLM 对账失败 → ⚠️ 本次跳过登记（不污染待办库）

匹配依据（match_basis）随结果输出到报告，保证人工可审计。
"""

import json
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MATCH_PROMPT = """你是策略池对账助手。下面给出系统策略池（注册表，含实现状态）和本周候选策略，
请判断每个候选对应池内哪一条，或是否为池内没有的全新策略。

## 策略池（注册表）
{registry_text}

## 本周候选策略
{candidates_text}

## 匹配规则
- 语义相同或高度相似即视为同一条，不要求名字一致（如"行业极端反转"≈"超跌反弹"）
- 候选是池内某策略的变体/具体化（核心逻辑相同、触发条件细化）也算同一条，并在依据中说明差异
- 确实是池内没有的新思路才 registry_id 填 null
- registry_id 只能取自上面列表中存在的 id，不得编造

## 输出格式（JSON 数组，每个候选一项，顺序与输入一致）
[
  {{"candidate": "候选策略名称（原文）", "registry_id": "池内id 或 null", "match_basis": "一句话匹配依据，需引用池内条目的名称或描述"}}
]

只输出JSON，不要其他内容。"""


def _format_registry_for_match(strategies: List[dict]) -> str:
    """将策略池格式化为对账用紧凑文本"""
    lines = []
    for s in strategies:
        impl_text = f"已实现: {s['existing_implementation']}" if s.get("existing_implementation") else "未实现"
        lines.append(
            f"- [{s['id']}] {s['name']}（{s.get('category', '未分类')}）| {impl_text} "
            f"| 适配: {', '.join(s.get('suitable_regimes', []))}\n"
            f"  描述: {s.get('description', '')}"
        )
    return "\n".join(lines)


def _format_candidates(candidates: List[dict]) -> str:
    """将候选策略格式化为对账用紧凑文本"""
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. {c.get('name', '')}（{c.get('category', '未分类')}）\n"
            f"   描述: {c.get('description', '')}"
        )
    return "\n".join(lines)


def _parse_json_list(raw: str) -> Optional[List[dict]]:
    """解析 LLM 返回的 JSON 数组（容忍 markdown 代码块包裹），失败返回 None"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def _match_via_llm(candidates_text: str, registry_text: str) -> Optional[List[dict]]:
    """一次批量调用 LLM 完成全部候选与池内的对账，失败返回 None"""
    from src.strategy_planner.llm_client import get_llm_client

    client = get_llm_client()
    if not client.available:
        logger.error("LLM 不可用，无法进行池内对账")
        return None
    prompt = MATCH_PROMPT.format(registry_text=registry_text, candidates_text=candidates_text)
    raw = client.chat("你是策略池对账助手。请严格按照要求的JSON格式输出。", prompt, 0.0)
    matches = _parse_json_list(raw)
    if matches is None:
        logger.warning(f"池内对账输出解析失败: {str(raw)[:200]}...")
    return matches


def _register_todo(cand: dict, registry_entry: Optional[dict], market_phase: str) -> dict:
    """登记待办；池内匹配的用池内规范名/描述，便于跨周按 registry_id 去重。"""
    from src.strategy_planner.strategy_todo import add_todo

    if registry_entry:
        payload = {
            "name": registry_entry["name"],
            "category": registry_entry.get("category", cand.get("category", "其他")),
            "description": registry_entry.get("description", cand.get("description", "")),
            "market_phase": market_phase,
            "recommend_reason": cand.get("reason", ""),
            "registry_id": registry_entry["id"],
            "doc_ref": [registry_entry["doc_ref"]] if registry_entry.get("doc_ref") else None,
        }
    else:
        payload = {
            "name": cand.get("name", "未命名策略"),
            "category": cand.get("category", "其他"),
            "description": cand.get("description", ""),
            "market_phase": market_phase,
            "recommend_reason": cand.get("reason", ""),
            "doc_ref": None,
        }
    return add_todo(payload)


def check_implementations(candidates: List[dict], market_phase: str = "") -> List[dict]:
    """
    Agent 2 主入口：批量对账候选策略与策略池注册表。

    Returns: 每个候选一项
        {"strategy": 名称, "status": implemented/todo_pool/todo_new/error,
         "registry_id": ..., "match_basis": ..., "evidence": ..., "todo_id": ...}
    """
    from src.strategy_planner.strategy_registry import get_all_strategies

    strategies = get_all_strategies()
    id_map = {s["id"]: s for s in strategies}
    matches = _match_via_llm(_format_candidates(candidates), _format_registry_for_match(strategies))

    # 按候选名建索引；LLM 微调过名称时按输入顺序兜底对齐
    match_by_name = {}
    if matches:
        for m in matches:
            if isinstance(m, dict) and m.get("candidate"):
                match_by_name[m["candidate"]] = m

    results = []
    for idx, cand in enumerate(candidates):
        name = cand.get("name", "")
        m = match_by_name.get(name)
        if m is None and matches and idx < len(matches) and isinstance(matches[idx], dict):
            m = matches[idx]

        if m is None:
            results.append({
                "strategy": name,
                "status": "error",
                "evidence": "LLM 对账输出缺失或解析失败，本次跳过登记",
            })
            continue

        rid = m.get("registry_id")
        entry = id_map.get(rid) if rid else None
        basis = m.get("match_basis", "")

        if entry and entry.get("existing_implementation"):
            results.append({
                "strategy": name,
                "status": "implemented",
                "registry_id": rid,
                "match_basis": basis,
                "evidence": f"池内 [{rid}] {entry['name']} 已实现: {entry['existing_implementation']}",
            })
        elif entry:
            todo = _register_todo(cand, entry, market_phase)
            results.append({
                "strategy": name,
                "status": "todo_pool",
                "registry_id": rid,
                "match_basis": basis,
                "todo_id": todo.get("id"),
                "evidence": f"池内 [{rid}] {entry['name']} 已登记但未实现，挂入待办",
            })
        else:
            todo = _register_todo(cand, None, market_phase)
            results.append({
                "strategy": name,
                "status": "todo_new",
                "match_basis": basis,
                "todo_id": todo.get("id"),
                "evidence": (
                    f"LLM 返回未知 registry_id [{rid}]，已按全新策略登记待办"
                    if rid else "策略池中无对应条目（全新策略），挂入待办"
                ),
            })
    return results


def list_todo_summary() -> str:
    """待办库摘要文本（报告用）"""
    from src.strategy_planner.strategy_todo import get_todos

    todos = get_todos(status="todo")
    if not todos:
        return "待办库为空，所有推荐策略均已有实现或无需实现。"
    lines = [f"当前待办策略 {len(todos)} 个："]
    for t in todos:
        ref = f"（文档: {', '.join(t['doc_ref'])}）" if t.get("doc_ref") else ""
        desc = t.get("description", "")
        if len(desc) > 60:
            desc = desc[:60] + "…"
        lines.append(f"- **{t.get('name')}** [{t.get('category', '未分类')}]{ref}：{desc}")
    return "\n".join(lines)
