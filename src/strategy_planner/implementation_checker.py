# -*- coding: utf-8 -*-
"""
===================================
实现检查器 — Agent 2
===================================

对 Agent 1 推荐的策略，先对照项目策略文档（strategy/*.md，权威定义），
再检查代码实现情况：

1. 代码有实现 → implemented（✅ 已有实现）
2. 策略文档有定义但无代码 → documented_only（📋 待办，带文档引用）
3. 文档无定义也无代码 → new（📋 待办，全新策略）
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent

# 项目策略文档目录（权威策略定义）
STRATEGY_DOCS_DIR = PROJECT_ROOT / "strategy"

# 搜索范围（业务代码，不含数据源/文档等非策略实现）
SEARCH_DIRS = ["src", "."]
EXCLUDE_DIRS = {".git", ".conda", "logs", "reports", "data", "docs", "wheels", "__pycache__", ".github", ".vscode", "data_provider", "strategy"}

# 策略名尾缀（从名称中剔除的泛化词，用于提取独特核心词）
NAME_SUFFIX_WORDS = [
    "策略", "增强", "买入", "卖出", "交易", "配置", "管理", "对冲",
    "轮动", "追涨", "回调", "突破", "回归", "防御", "进攻", "指数",
]

# 泛化词（拆词时剔除，避免误匹配）
GENERIC_WORDS = {
    "行业", "市场", "指数", "波动", "动量", "成长", "趋势", "区间",
    "下的", "之间", "在", "与", "和", "的", "下", "类",
}

# 每个类别对应的关键词（用于匹配现有实现）
CATEGORY_KEYWORDS = {
    "趋势跟踪": ["趋势", "trend", "均线", "ma5", "ma10", "pullback", "回调"],
    "资产配置": ["配置", "allocation", "pe分位", "估值", "gate", "定投"],
    "防御策略": ["防御", "红利", "dividend", "高股息", "黄金", "gold", "现金", "逆回购"],
    "震荡策略": ["震荡", "网格", "grid", "波段", "trading"],
    "反转策略": ["反转", "超跌", "反弹", "oversold", "bounce", "reversal"],
    "动量策略": ["动量", "momentum", "轮动", "rotation", "追涨"],
    "对冲策略": ["对冲", "hedge", "套利", "arbitrage"],
    "事件驱动": ["事件", "event", "公告", "龙虎榜", "涨停"],
    "其他": [],
}


def _extract_core_keywords(name: str) -> List[str]:
    """从策略名提取独特核心词（剔除尾缀/泛化词），用于精确匹配。"""
    core = []
    for part in re.split(r"[（(]|[\s_-]+", name or ""):
        part = part.strip()
        if not part or len(part) < 2:
            continue
        for suffix in NAME_SUFFIX_WORDS:
            if part.endswith(suffix):
                part = part[: -len(suffix)]
                break
        if not part or len(part) < 2:
            continue
        if part not in core:
            core.append(part)
        # 长片段拆 2 字滑窗词（剔除泛化词；注释/docstring 过滤在搜索阶段兜底）
        if len(part) >= 4:
            for i in range(len(part) - 1):
                sub = part[i : i + 2]
                if sub not in GENERIC_WORDS and sub not in core:
                    core.append(sub)
    return core


def _iter_python_files() -> List[Path]:
    """遍历项目内的 Python 文件"""
    files = []
    for d in SEARCH_DIRS:
        base = PROJECT_ROOT if d == "." else PROJECT_ROOT / d
        if not base.exists():
            continue
        for root, dirs, fnames in os.walk(base):
            root_path = Path(root)
            rel_parts = set(root_path.relative_to(PROJECT_ROOT).parts)
            if rel_parts & EXCLUDE_DIRS:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in fnames:
                if fn.endswith(".py"):
                    files.append(root_path / fn)
    return files


def _extract_docstring_lines(content: str) -> set:
    """提取 docstring 占用的行号集合（三引号块内）。"""
    doc_lines = set()
    in_doc = False
    quote_char = ""
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not in_doc:
            if stripped.startswith(('"""', "'''")):
                in_doc = True
                quote_char = stripped[:3]
                # 单行 docstring
                if stripped.count(quote_char) >= 2 or (len(stripped) > 3 and stripped.endswith(quote_char)):
                    in_doc = False
                    continue
                doc_lines.add(i)
        else:
            doc_lines.add(i)
            if quote_char in stripped:
                in_doc = False
    return doc_lines


def _search_keywords(keywords: List[str]) -> List[Dict[str, str]]:
    """在项目源码中搜索关键词，返回命中位置列表"""
    hits = []
    for fp in _iter_python_files():
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        doc_lines = _extract_docstring_lines(content)
        for kw in keywords:
            if not kw:
                continue
            for m in re.finditer(re.escape(kw), content, re.IGNORECASE):
                line_no = content[: m.start()].count("\n") + 1
                if line_no in doc_lines:
                    continue
                line = content.splitlines()[line_no - 1].strip()[:100] if line_no <= len(content.splitlines()) else ""
                hits.append({
                    "file": str(fp.relative_to(PROJECT_ROOT)),
                    "line": line_no,
                    "keyword": kw,
                    "line_content": line,
                })
    return hits


def _search_strategy_docs(keywords: List[str], core_keywords: List[str]) -> List[str]:
    """
    在策略文档（strategy/*.md）中搜索关键词，返回命中的文档文件名列表。

    判定：核心词命中任意文档即算该文档定义了此策略；
    若核心词一个都没命中，即使类别词命中也不计（避免"市场""趋势"等泛词误匹配）。
    """
    doc_hits = []
    if not STRATEGY_DOCS_DIR.exists():
        return doc_hits
    for fp in STRATEGY_DOCS_DIR.glob("*.md"):
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        lowered = content.lower()
        if core_keywords and not any(kw.lower() in lowered for kw in core_keywords):
            continue
        for kw in keywords:
            if kw and kw.lower() in lowered:
                doc_hits.append(str(fp.relative_to(PROJECT_ROOT)))
                break
    return doc_hits


def check_implementation(strategy: dict) -> Dict[str, any]:
    """
    检查策略的实现情况（三态判定）。

    匹配逻辑：
    1. 核心词（策略名去除尾缀后的独特词）优先 —— 必须命中才算数
    2. 类别关键词作为补充（核心词不足时）
    3. 忽略注释行命中（# 开头）

    Returns:
        {
            "implemented": bool,       # 代码已实现
            "documented": bool,        # 策略文档（strategy/*.md）已有定义
            "doc_files": [str],        # 命中的策略文档
            "hits": [dict],            # 代码命中位置
            "evidence": "简要说明",
        }
    """
    name = strategy.get("name", "")
    category = strategy.get("category", "")

    # 核心词：名称独特词 + 类别关键词
    core_keywords = _extract_core_keywords(name)
    category_kws = CATEGORY_KEYWORDS.get(category, [])
    keywords = list(dict.fromkeys(core_keywords + category_kws))

    # 1. 代码搜索
    hits = _search_keywords(keywords)
    # 过滤注释命中：整行注释（# 开头）与行内注释（关键词出现在 # 之后）
    filtered = []
    for h in hits:
        line = h["line_content"]
        if line.lstrip().startswith(("#", "//", "\"\"\"", "'''")):
            continue
        # 行内注释：关键词位置在 # 之后 → 视为注释
        hash_pos = line.find("#")
        if hash_pos >= 0 and line[:hash_pos].count(h["keyword"]) == 0:
            continue
        filtered.append(h)
    # 排除策略规划器自身文件（注册表/待办库/本模块不是真实实现）
    hits = [h for h in filtered if "strategy_planner" not in h["file"]]

    # 2. 策略文档搜索
    doc_files = _search_strategy_docs(keywords, core_keywords)

    # 3. 三态判定
    hit_files = {h["file"] for h in hits}
    core_hits = [h for h in hits if h["keyword"] in core_keywords]
    core_failed = bool(core_keywords and not core_hits)

    # 代码命中且核心词成立 → 已实现
    if hits and not core_failed:
        evidence = f"命中 {len(hit_files)} 个源码文件: {', '.join(sorted(hit_files)[:5])}"
        return {
            "implemented": True,
            "documented": bool(doc_files),
            "doc_files": doc_files,
            "hits": hits,
            "evidence": evidence,
        }

    # 未实现：区分"文档有定义"与"全新策略"
    if hits and doc_files:
        evidence = f"策略文档已定义（{', '.join(doc_files)}），代码无实质实现"
    elif hits:
        evidence = f"策略名核心词 {core_keywords} 未命中实际代码"
    elif doc_files:
        evidence = f"策略文档已定义（{', '.join(doc_files)}），未找到代码实现"
    else:
        evidence = "未在项目源码与策略文档中找到相关内容（全新策略）"

    return {
        "implemented": False,
        "documented": bool(doc_files),
        "doc_files": doc_files,
        "hits": hits,
        "evidence": evidence,
    }


def ensure_strategy_implementation(strategy: dict) -> Dict[str, any]:
    """
    Agent 2 主入口：检查推荐策略的实现情况，未实现则登记待办。

    Returns:
        {"implemented": bool, "evidence": str, "added_to_todo": bool}
    """
    result = check_implementation(strategy)
    if result["implemented"]:
        logger.info(f"策略 [{strategy.get('name')}] 已有实现: {result['evidence']}")
        return {"implemented": True, "evidence": result["evidence"], "added_to_todo": False}

    from src.strategy_planner.strategy_todo import add_todo

    todo = add_todo({
        "name": strategy.get("name", "未命名策略"),
        "category": strategy.get("category", "其他"),
        "description": strategy.get("description", ""),
        "market_phase": strategy.get("market_phase", ""),
        "recommend_reason": strategy.get("why_now", strategy.get("recommend_reason", "")),
        "doc_ref": result.get("doc_files", []) or None,
    })
    return {
        "implemented": False,
        "evidence": result["evidence"],
        "added_to_todo": True,
        "todo_id": todo.get("id"),
        "documented": result.get("documented", False),
    }


def list_todo_summary() -> str:
    """待办库摘要文本（报告用）"""
    from src.strategy_planner.strategy_todo import get_todos

    todos = get_todos(status="todo")
    if not todos:
        return "待办库为空，所有推荐策略均已有实现或无需实现。"
    lines = [f"当前待办策略 {len(todos)} 个："]
    for t in todos:
        ref = f"（文档: {', '.join(t['doc_ref'])}）" if t.get("doc_ref") else ""
        lines.append(f"- **{t.get('name')}** [{t.get('category', '未分类')}]{ref}：{t.get('description', '')[:60]}")
    return "\n".join(lines)
