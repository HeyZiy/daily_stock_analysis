# -*- coding: utf-8 -*-
"""
===================================
ETF 长期配置 — 估值门控
===================================

逆向配置的三维信号：
1. PE 分位 — A 股整体 PE 近 5 年百分位（主信号）
2. 股债性价比 — 沪深300 盈利收益率 vs 10 年期国债收益率（辅助 ±5%）
3. 恐慌信号 — 上证连续下跌 + 缩量 → 恐慌加仓机会（辅助 +5%）

最终 offset = pe_offset + bond_spread_bonus + panic_bonus
"""

import logging
from typing import Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# PE 分位 → 基础偏移（主信号）
PE_OFFSET_TABLE = [
    (20, 0.15),    # 极度低估
    (40, 0.10),    # 低估
    (60, 0.00),    # 合理
    (80, -0.10),   # 高估
    (100, -0.20),  # 极度高估
]

# 股债性价比 → 修正量（辅助信号）
# 盈利收益率 = 1/PE，与 10 年期国债收益率比较
BOND_SPREAD_TABLE = [
    (1.0, -0.05),   # 利差 < 1% → 股票性价比极低，减 5%
    (2.0, -0.02),   # 利差 1-2% → 略贵
    (3.0,  0.00),   # 利差 2-3% → 中性
    (4.0,  0.02),   # 利差 3-4% → 偏便宜
    (5.0,  0.05),   # 利差 > 5% → 明显便宜，加 5%
]

# 恐慌信号修正（仅正向，不加负向）
PANIC_BONUS = 0.05

FALLBACK_BOND_YIELD = 1.75  # 默认国债收益率（百分数）


def _pe_to_offset(pe_pct: float) -> float:
    for threshold, offset in PE_OFFSET_TABLE:
        if pe_pct < threshold:
            return offset
    return -0.20


def _fetch_pe_data():
    """获取 A 股整体 PE 历史数据"""
    import akshare as ak

    df = None
    for symbol in ["全市场", "全部A股", "all"]:
        try:
            df = ak.stock_a_pe_lg(symbol=symbol)
            if df is not None and not df.empty and "average_pe" in df.columns:
                break
        except Exception:
            continue

    if df is None or df.empty:
        try:
            df = ak.stock_a_pe_lg(symbol="沪深300")
        except Exception:
            pass

    if df is None or df.empty:
        return 50.0, 0.0, None, "无法获取 PE 数据"

    df = df.sort_values("date").reset_index(drop=True)
    df = df.tail(1250)
    if len(df) < 250:
        return 50.0, 0.0, None, "PE 数据不足"

    current_pe = float(df["average_pe"].iloc[-1])
    if current_pe <= 0:
        return 50.0, 0.0, None, "PE 值异常"

    percentile = float((df["average_pe"] <= current_pe).mean() * 100)
    return percentile, current_pe, df, ""


def _fetch_bond_yield() -> float:
    """获取中国 10 年期国债收益率"""
    try:
        import akshare as ak
        df = ak.bond_china_yield()
        if df is not None and not df.empty:
            row = df[df["曲线名称"].str.contains("10年", na=False)]
            if not row.empty:
                return float(row.iloc[-1]["收益率"] or row.iloc[-1].iloc[-1])
    except Exception:
        pass

    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        if df is not None and not df.empty:
            col = [c for c in df.columns if "10" in c and "中" in c]
            if col:
                return float(df[col[0]].iloc[-1])
    except Exception:
        pass

    return FALLBACK_BOND_YIELD


def _compute_bond_spread(current_pe: float) -> Tuple[float, str]:
    """计算股债性价比修正量"""
    bond_yield = _fetch_bond_yield()
    if current_pe <= 0:
        return 0.0, f"国债{bond_yield:.2f}% (PE异常，跳过股债)"
    # 这里我打印变量名时可能有bug...
    earnings_yield = (1.0 / current_pe) * 100
    spread = earnings_yield - bond_yield

    bonus = 0.0
    for threshold, b in BOND_SPREAD_TABLE:
        if spread < threshold:
            bonus = b
            break

    label = "极具性价比" if bonus >= 0.05 else \
            "偏便宜" if bonus >= 0.02 else \
            "中性" if bonus >= 0.00 else \
            "偏贵" if bonus >= -0.02 else "太贵"
    detail = (
        f"盈利收益率{earnings_yield:.2f}% - 国债{bond_yield:.2f}% = 利差{spread:.2f}% ({label})"
    )

    return bonus, detail


def _check_panic() -> Tuple[float, str]:
    """检查恐慌信号：连续下跌 + 缩量"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or df.empty:
            return 0.0, ""
        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < 20:
            return 0.0, ""

        recent = df.tail(10)
        closes = recent["close"].values
        volumes = recent["volume"].values if "volume" in recent.columns else []

        # 条件1：最近 3 天连续下跌
        consecutive_drops = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                consecutive_drops += 1
            else:
                break

        # 条件2：成交量持续萎缩（近3日均量 < 近20日均量 × 0.8）
        vol_shrink = False
        if len(volumes) >= 3 and len(df) >= 20:
            avg_vol_3 = volumes[-3:].mean()
            avg_vol_20 = df["volume"].iloc[-20:].mean()
            if avg_vol_20 > 0 and avg_vol_3 < avg_vol_20 * 0.8:
                vol_shrink = True

        if consecutive_drops >= 3 and vol_shrink:
            return PANIC_BONUS, f"恐慌信号：连跌{consecutive_drops}天 + 缩量，逆势加仓"
        elif consecutive_drops >= 3:
            return 0.0, f"连跌{consecutive_drops}天（量能未收缩，不触发恐慌）"
        return 0.0, ""
    except Exception:
        return 0.0, ""


def check_allocation_gate() -> Tuple[float, float, float, str]:
    """估值门控主入口

    Returns:
        equity_offset  — 最终权益偏移 (-0.40 ~ 0.20)
        pe_percentile  — PE 近5年分位 (0-100)
        current_pe     — 当前 PE
        summary        — 多行可读摘要
    """
    # ── 信号 1：PE 分位（主信号） ──
    pe_pct, current_pe, pe_df, error = _fetch_pe_data()
    if error:
        logger.warning(f"PE 数据获取失败: {error}")
        return 0.0, 50.0, 0.0, "⚠️ PE 数据获取失败（{}），维持中性配置".format(error)

    base_offset = _pe_to_offset(pe_pct)

    pe_level = "极度低估" if pe_pct < 20 else \
               "低估" if pe_pct < 40 else \
               "合理" if pe_pct < 60 else \
               "高估" if pe_pct < 80 else "极度高估"

    lines = [
        "=" * 50,
        f"📊 信号1 — PE 分位",
        f"  当前PE: {current_pe:.1f} | 近5年 {pe_pct:.0f}% 分位 | {pe_level}",
        f"  基础偏移: {'+' if base_offset >= 0 else ''}{base_offset*100:.0f}%",
    ]

    # ── 信号 2：股债性价比 ──
    bond_bonus, bond_detail = _compute_bond_spread(current_pe)
    lines.append(f"📊 信号2 — 股债性价比")
    lines.append(f"  {bond_detail} → 修正 {'+' if bond_bonus >= 0 else ''}{bond_bonus*100:.0f}%")

    # ── 信号 3：恐慌信号 ──
    panic_bonus, panic_detail = _check_panic()
    if panic_detail:
        lines.append(f"📊 信号3 — 恐慌信号")
        lines.append(f"  {panic_detail} → 修正 +{panic_bonus*100:.0f}%")

    # ── 汇总 ──
    final_offset = base_offset + bond_bonus + panic_bonus
    final_offset = max(-0.40, min(0.20, final_offset))
    final_offset = round(final_offset, 2)

    lines.append("─" * 50)
    direction = "超配 → 多买" if final_offset > 0 else \
                "减配 → 少买" if final_offset < 0 else "中性 → 不动"
    lines.append(f"✅ 最终权益偏移: {'+' if final_offset >= 0 else ''}{final_offset*100:.0f}%  |  {direction}")

    summary = "\n".join(lines)
    logger.info(f"PE分位:{pe_pct:.0f}% 基础:{base_offset:+.0%} 股债:{bond_bonus:+.0%} 恐慌:{panic_bonus:+.0%} → 最终:{final_offset:+.0%}")
    return final_offset, pe_pct, current_pe, summary
