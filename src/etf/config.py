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
# 核心仓位：长期持有，来源=有知有行基准（适配规则见 strategy/etf_allocation.md 第二节），
# 半年人工对齐一次。权重为总资产占比；"现金"桶吸收卫星仓与其他账户的资金。
# 卫星仓（行业动量轮动，标的集见 get_rotation_universe_codes）独立于核心基准，
# 动态扫描 ETF_INDUSTRY_MAP，其持仓被"现金（以及其他账户）"桶吸收，不产生核心偏离。

CORE_BASELINE: List[AssetAllocation] = [
    # ── A股宽基 ──
    AssetAllocation("563360", "A500ETF",                AssetType.EQUITY, 0.17, 8),
    AssetAllocation("159680", "中证1000增强ETF",         AssetType.EQUITY, 0.01, 6),
    AssetAllocation("515180", "红利ETF",                AssetType.EQUITY, 0.14, 10),
    # ── 海外 ──
    AssetAllocation("513100", "纳指ETF",                AssetType.EQUITY, 0.05, 9),
    AssetAllocation("513500", "标普500ETF",              AssetType.EQUITY, 0.05, 9),
    AssetAllocation("513380", "恒生ETF",                AssetType.EQUITY, 0.10, 5),
    # ── 行业/主题 ──
    AssetAllocation("159938", "医药ETF",                AssetType.EQUITY, 0.04, 4),
    AssetAllocation("516560", "养老ETF",                AssetType.EQUITY, 0.02, 7),
    AssetAllocation("159928", "消费ETF",                AssetType.EQUITY, 0.08, 7),
    # ── 黄金 ──
    AssetAllocation("159934", "黄金ETF",                AssetType.GOLD,   0.05, 11),
    # ── 现金（国债逆回购，自动理财，不买货基） ──
    AssetAllocation("CASH",   "现金/逆回购",              AssetType.CASH,   0.29, 13),
]

# 再平衡模块使用核心仓位
NEUTRAL_BASELINE = CORE_BASELINE

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


def get_rotation_universe_codes() -> set:
    """卫星仓（非核心）标的代码集（剔除核心基准代码，避免与核心仓资金口径重叠）。

    核心仓再平衡以"核心资金 = 总资产 − 卫星持仓市值"为口径，
    卫星标的独立预算、独立进出，不参与核心偏离计算。
    """
    try:
        from src.etf.amazing_factors import ETF_INDUSTRY_MAP
    except Exception:
        return set()
    baseline_codes = {a.code for a in CORE_BASELINE}
    return set(ETF_INDUSTRY_MAP) - baseline_codes



def get_rebalance_threshold(gate_state: str) -> float:
    """根据 gate 状态返回再平衡阈值（gate 的残值：只选阈值松紧，不产生仓位动作）"""
    if gate_state in ("trending_up", "weak_up"):
        return REBALANCE_LOOSE_THRESHOLD
    if gate_state in ("trending_down", "chaos"):
        return REBALANCE_TIGHT_THRESHOLD
    return REBALANCE_SINGLE_THRESHOLD

