# -*- coding: utf-8 -*-
"""
===================================
策略待办库 — 待实现的策略清单
===================================

Agent 2（实现检查器）发现推荐的策略在项目中没有成熟代码时，
将其登记到待办库，等待后续实现。

格式：
{
  "todos": [
    {
      "id": "todo_2026-08-09_0",
      "name": "策略名称",
      "category": "策略类别",
      "description": "策略描述",
      "recommended_at": "2026-08-09",
      "market_phase": "推荐时的市场阶段",
      "recommend_reason": "推荐理由",
      "status": "todo",          # todo / done / cancelled
      "implementation": null     # 完成后填实现文件
    }
  ]
}
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
TODO_PATH = PROJECT_ROOT / "data" / "strategy_todo.json"


def _load_todos() -> dict:
    if TODO_PATH.exists():
        try:
            return json.loads(TODO_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("策略待办库损坏，重置为空")
    return {"todos": []}


def _save_todos(data: dict):
    TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    TODO_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_todos(status: Optional[str] = None) -> List[dict]:
    """获取待办列表（可按状态过滤）"""
    todos = _load_todos().get("todos", [])
    if status:
        todos = [t for t in todos if t.get("status") == status]
    return todos


def add_todo(strategy: dict) -> dict:
    """登记一个新待办策略（按名称去重）"""
    data = _load_todos()
    todos = data.setdefault("todos", [])
    name = strategy.get("name", "")
    for t in todos:
        if t.get("name") == name and t.get("status") == "todo":
            return t  # 已在待办，不重复登记

    strategy.setdefault("id", f"todo_{date.today().isoformat()}_{len(todos)}")
    strategy.setdefault("recommended_at", date.today().isoformat())
    strategy.setdefault("status", "todo")
    strategy.setdefault("implementation", None)
    todos.append(strategy)
    _save_todos(data)
    logger.info(f"新增策略待办: {strategy['name']} (id={strategy['id']})")
    return strategy


def mark_done(todo_id: str, implementation: str = "") -> Optional[dict]:
    """标记待办已完成"""
    data = _load_todos()
    for t in data.get("todos", []):
        if t.get("id") == todo_id:
            t["status"] = "done"
            if implementation:
                t["implementation"] = implementation
            _save_todos(data)
            logger.info(f"待办已完成: {t['name']}")
            return t
    return None


# 初始化：如果文件不存在则创建
if not TODO_PATH.exists():
    _save_todos({"todos": []})
