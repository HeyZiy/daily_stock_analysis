# -*- coding: utf-8 -*-
"""
===================================
ETF 长期配置 — 中性基准
===================================

中性基准反映长期信念，半年至一年审视一次。每日 gate 状态只在基准上做战术偏移，
不改变基准本身。

类别归属：
  equity    — 权益类（进攻弹性来源）
  bond      — 债券类（安全垫）
  gold      — 黄金（极端风险对冲，不减仓）
  cash      — 现金/货币（弹药 + 流动性缓冲）
"""

from dataclasses import dataclass
from typing import List

# ── 类别枚举 ──

class AssetType:
    EQUITY = "equity"
    BOND = "bond"
    GOLD = "gold"
    CASH = "cash"


@dataclass
class AssetAllocation:
    code: str           # 证券代码（6 位）
    name: str           # 名称
    asset_type: str     # AssetType
    neutral_weight: float  # 中性基准权重（0.0 ~ 1.0）
    volatility_rank: int   # 波动率排名（1=最高波动，用于减仓优先级）


# ── 中性基准配置 ──

NEUTRAL_BASELINE: List[AssetAllocation] = [
    AssetAllocation("510300", "沪深300ETF",    AssetType.EQUITY, 0.20, 3),
    AssetAllocation("510500", "中证500ETF",    AssetType.EQUITY, 0.10, 4),
    AssetAllocation("159915", "创业板50ETF",    AssetType.EQUITY, 0.05, 1),
    AssetAllocation("588000", "科创50ETF",     AssetType.EQUITY, 0.05, 2),
    AssetAllocation("510880", "红利ETF",       AssetType.EQUITY, 0.05, 6),
    AssetAllocation("513100", "纳指ETF",       AssetType.EQUITY, 0.05, 5),
    AssetAllocation("159920", "恒生ETF",       AssetType.EQUITY, 0.05, 3),
    AssetAllocation("518880", "黄金ETF",       AssetType.GOLD,   0.05, 7),
    AssetAllocation("511010", "国债ETF",       AssetType.BOND,   0.15, 8),
    AssetAllocation("511880", "货币ETF",       AssetType.CASH,   0.25, 9),
]

# Gate 状态 → 权益类战术偏移（正数=加权益减现金，负数=减权益加现金）
GATE_OFFSET: dict = {
    "trending_up":    0.15,
    "weak_up":        0.10,
    "sideways":       0.00,
    "trending_down": -0.20,
    "chaos":         -0.30,
}

# hard_intercept 状态下额外偏移（叠加到 gate 偏移上）
HARD_INTERCEPT_EXTRA = -0.10

# 减仓优先级：按 volatility_rank 从高到低（创业板先减，国债/现金后减）
# gold 和 bond 在 trending_down/chaos/hard_intercept 时不减
PROTECTED_TYPES = {AssetType.GOLD, AssetType.BOND}

# 再平衡阈值
REBALANCE_SINGLE_THRESHOLD = 0.05     # 单类偏离 > 5% 触发
REBALANCE_TOTAL_THRESHOLD = 0.15      # 所有偏离绝对值之和 > 15% 强制触发
REBALANCE_LOOSE_THRESHOLD = 0.08      # trending_up/weak_up 时的放宽阈值
REBALANCE_TIGHT_THRESHOLD = 0.03      # trending_down/chaos/hard_intercept 时的收紧阈值
MIN_TRADE_DEVIATION = 0.02            # 忽略 < 2% 的碎股偏差


def get_neutral_baseline() -> List[AssetAllocation]:
    return NEUTRAL_BASELINE


def get_equity_total_weight() -> float:
    return sum(a.neutral_weight for a in NEUTRAL_BASELINE if a.asset_type == AssetType.EQUITY)


def get_gate_offset(gate_state: str, hard_intercept: bool) -> float:
    """返回权益类偏移量（正数=加权益，负数=减权益）"""
    offset = GATE_OFFSET.get(gate_state, 0.0)
    if hard_intercept:
        offset += HARD_INTERCEPT_EXTRA
    return max(-0.40, min(0.15, offset))  # 限制在 [-40%, +15%] 范围内


def get_rebalance_threshold(gate_state: str) -> float:
    """根据 gate 状态返回再平衡阈值"""
    if gate_state in ("trending_up", "weak_up"):
        return REBALANCE_LOOSE_THRESHOLD
    if gate_state in ("trending_down", "chaos"):
        return REBALANCE_TIGHT_THRESHOLD
    return REBALANCE_SINGLE_THRESHOLD
