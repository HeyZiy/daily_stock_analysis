# -*- coding: utf-8 -*-
"""
===================================
策略注册表 — 自进化策略池
===================================

策略以 JSON 文件存储，LLM 可以提议新增策略。
格式：
{
  "strategies": [
    {
      "id": "trend_pullback",
      "name": "趋势回调买入",
      "category": "趋势跟踪",
      "description": "在均线多头排列的股票中，等待回调到MA5/MA10时买入",
      "suitable_regimes": ["trending_up", "weak_up"],
      "existing_implementation": "trend_analysis.py:detect_pullback_signals",
      "source": "builtin",
      "created_at": "2026-08-08",
      "added_by": "system"
    }
  ],
  "pending_strategies": []
}
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
REGISTRY_PATH = PROJECT_ROOT / "data" / "strategy_registry.json"

DEFAULT_STRATEGIES = [
    {
        "id": "trend_pullback",
        "name": "趋势回调买入",
        "category": "趋势跟踪",
        "description": "在均线多头排列的股票中，检测回调至MA5/MA10的买点，评分后排序",
        "suitable_regimes": ["trending_up", "weak_up"],
        "existing_implementation": "src/analysis/strategy/signal_detector.py",
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "etf_pe_allocation",
        "name": "ETF PE估值配置",
        "category": "资产配置",
        "description": "根据A股整体PE分位和股债利差，动态调整股债比例",
        "suitable_regimes": ["trending_up", "weak_up", "sideways", "trending_down"],
        "existing_implementation": "etf_observe.py + src/etf/allocation_gate.py + src/etf/rebalancer.py",
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "sector_rotation",
        "name": "行业轮动",
        "category": "动量策略",
        "description": "扫描全行业ETF清单，按20日+5日动量+均线+量能打分排名，仅观察不交易",
        "suitable_regimes": ["trending_up", "weak_up"],
        "existing_implementation": "src/etf/sector_rotation.py（观察工具，周报排名）",
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "rocket_etf_breakout",
        "name": "量价爆发突破（红色火箭·ETF版）",
        "category": "动量策略",
        "description": "卫星仓战术策略：在行业ETF清单中捕捉放量突破首日（60日新高/平台突破+涨幅3%-9.5%，量≥5日均量×2且≥20日均量×1.5，行业PE分位<60%），评分≥70买入，10%预算最多2只等权。退出：-7%止损、+15%减半/+25%清仓、3日破MA5、10日不新高、PE≥60%或硬拦截全清。",
        "suitable_regimes": ["trending_up", "weak_up"],
        "existing_implementation": "src/etf/rocket_breakout.py + etf_observe.py 统一执行批次",
        "source": "user_suggested",
        "created_at": "2026-08-15",
        "added_by": "user",
        "doc_ref": "strategy/rocket_breakout.md",
    },
    {
        "id": "high_dividend_defense",
        "name": "高股息防御",
        "category": "防御策略",
        "description": "在市场弱势时配置高股息/红利类资产，获取稳定分红收益",
        "suitable_regimes": ["trending_down", "chaos", "sideways"],
        "existing_implementation": "src/etf/config.py 红利ETF(515180)",
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "cash_management",
        "name": "现金管理（逆回购/货基）",
        "category": "防御策略",
        "description": "空仓等待，资金配置国债逆回购获取无风险收益",
        "suitable_regimes": ["chaos"],
        "existing_implementation": "src/etf/config.py 现金仓位(CASH 29%)",
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "grid_trading",
        "name": "网格/波段交易",
        "category": "震荡策略",
        "description": "在震荡区间内低买高卖，赚取波动收益",
        "suitable_regimes": ["sideways"],
        "existing_implementation": None,
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "momentum_chasing",
        "name": "动量追涨",
        "category": "动量策略",
        "description": "追买近期涨幅靠前的强势板块龙头股",
        "suitable_regimes": ["trending_up"],
        "existing_implementation": None,
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "oversold_bounce",
        "name": "超跌反弹",
        "category": "反转策略",
        "description": "在急跌后捕捉超跌反弹机会，快进快出",
        "suitable_regimes": ["trending_down"],
        "existing_implementation": None,
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
    {
        "id": "gold_commodity_hedge",
        "name": "黄金/商品对冲",
        "category": "对冲策略",
        "description": "配置黄金、商品ETF对冲系统性风险",
        "suitable_regimes": ["trending_down", "chaos"],
        "existing_implementation": "src/etf/config.py 黄金ETF(159934)",
        "source": "builtin",
        "created_at": "2026-08-08",
        "added_by": "system",
    },
]


def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("策略注册表损坏，使用默认配置")
    return {"strategies": list(DEFAULT_STRATEGIES), "pending_strategies": []}


def _save_registry(data: dict):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    registry_path_str = str(REGISTRY_PATH)
    REGISTRY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_all_strategies() -> List[dict]:
    """获取所有已审批的策略"""
    data = _load_registry()
    return data.get("strategies", [])


def get_pending_strategies() -> List[dict]:
    """获取 LLM 提议但待审批的新策略"""
    data = _load_registry()
    return data.get("pending_strategies", [])


def add_pending_strategy(strategy: dict):
    """LLM 提议新策略，加入待审批列表"""
    data = _load_registry()
    strategy.setdefault("id", f"llm_suggested_{date.today().isoformat()}_{len(data.get('pending_strategies', []))}")
    strategy.setdefault("source", "llm_suggested")
    strategy.setdefault("created_at", date.today().isoformat())
    strategy.setdefault("added_by", "strategy_planner")
    strategy.setdefault("existing_implementation", None)
    pending = data.setdefault("pending_strategies", [])
    # 去重
    existing_ids = {s["id"] for s in pending}
    if strategy["id"] not in existing_ids:
        pending.append(strategy)
        _save_registry(data)
        logger.info(f"新增待审批策略: {strategy['name']} (id={strategy['id']})")
    return strategy


def approve_strategy(strategy_id: str) -> Optional[dict]:
    """批准待审批策略，移入正式策略列表"""
    data = _load_registry()
    pending = data.get("pending_strategies", [])
    for i, s in enumerate(pending):
        if s["id"] == strategy_id:
            pending.pop(i)
            data["strategies"].append(s)
            _save_registry(data)
            logger.info(f"已批准策略: {s['name']}")
            return s
    return None


def remove_strategy(strategy_id: str) -> Optional[dict]:
    """移除策略"""
    data = _load_registry()
    for section in ["strategies", "pending_strategies"]:
        items = data.get(section, [])
        for i, s in enumerate(items):
            if s["id"] == strategy_id:
                removed = items.pop(i)
                _save_registry(data)
                logger.info(f"已移除策略: {removed['name']}")
                return removed
    return None


def format_strategies_for_llm(strategies: List[dict]) -> str:
    """将策略列表格式化为 LLM 可读文本"""
    lines = []
    for i, s in enumerate(strategies, 1):
        lines.append(
            f"{i}. **{s['name']}** [{s.get('category', '未分类')}]\n"
            f"   - 描述: {s['description']}\n"
            f"   - 适配市场: {', '.join(s.get('suitable_regimes', []))}\n"
            f"   - 是否已实现: {'是' if s.get('existing_implementation') else '否'}\n"
            f"   - 来源: {s.get('source', 'unknown')}"
        )
    return "\n\n".join(lines)


# 初始化：如果文件不存在则创建
if not REGISTRY_PATH.exists():
    _save_registry({"strategies": list(DEFAULT_STRATEGIES), "pending_strategies": []})
    logger.info("已创建初始策略注册表")
