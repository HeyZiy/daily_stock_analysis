# -*- coding: utf-8 -*-
"""
趋势跟踪日报 — Markdown 报告生成

接收 TechnicalSignal 和市场环境数据，返回格式化 Markdown 字符串。

使用模型：T日盘后出信号 → T+1观察开盘/盘中走势 → T+1尾盘决定是否介入。
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from src.analysis.strategy.signal_detector import TechnicalSignal

logger = logging.getLogger(__name__)

REGIME_DESC = {
    "trending_up":   "📈 趋势上行 — 均线多头排列",
    "weak_up":       "🌤️ 弱上行 — 站上 MA20 但非标准多头排列",
    "sideways":      "➡️ 震荡横盘 — 紧贴 MA20 震荡",
    "trending_down": "📉 趋势下行 — 均线空头排列",
    "chaos":         "🌪️ 混沌 — 方向不明",
}


def _build_action_guide(s: TechnicalSignal) -> dict:
    """
    为信号生成次日操作指引。

    Returns:
        dict 包含 observation（观察要点）、confirmation（确认条件）、
        invalidation（失效条件）、sizing（仓位建议）
    """
    guide = {}

    if s.signal_type == 'pullback_ma5':
        if s.pct_change > 5.0:
            guide['observation'] = "⚠️ 当日涨幅较大，非典型回踩。若次日高开>3%则放弃，平开/小幅低开可观察"
            guide['confidence'] = "低"
        elif s.pct_change < -3.0:
            guide['observation'] = "当日跌幅较大，观察次日能否止跌企稳。若继续阴线下跌则放弃"
            guide['confidence'] = "低"
        else:
            guide['observation'] = "观察次日开盘方向：若平开/小幅高开且早盘站稳MA5，信号有效可关注；若低开低走跌破MA5，放弃等待"
            guide['confidence'] = "中"

        guide['confirmation'] = f"次日收盘价 >= MA5({s.ma5:.2f}) 且量比 < 1.2"
        guide['invalidation'] = f"次日收盘价 < MA5({s.ma5:.2f}) * 0.99 或放量下跌(pct < -3%)"
        guide['sizing'] = "正常仓位(50%)"

    elif s.signal_type == 'pullback_ma10':
        guide['observation'] = "观察次日弱转强：需收盘站上MA5(至少触碰)，若继续在MA5-MA10之间弱势震荡则等待"
        guide['confidence'] = "低"
        guide['confirmation'] = f"次日收盘站上MA5({s.ma5:.2f}) 且量比 < 1.0"
        guide['invalidation'] = f"次日收盘跌破MA10({s.ma10:.2f}) 或继续缩量阴跌"
        guide['sizing'] = "半仓(25%)或观望"

    # 根据市场环境调节仓位
    if s.regime_note:
        if "下行" in s.regime_note:
            guide['sizing'] = "轻仓(20%)或放弃"
        elif "震荡" in s.regime_note:
            guide['sizing'] = "正常仓位(50%)"

    # 根据有效评分进一步调节
    if s.effective_score >= 80:
        guide['confidence'] = "高"
        if "轻仓" not in guide['sizing']:
            guide['sizing'] = "正常仓位(50%)"
    elif s.effective_score >= 60:
        guide['confidence'] = "中"
    else:
        guide['confidence'] = "低"
        guide['sizing'] = "观望或放弃"

    return guide


def _format_rank_table(signals: List[TechnicalSignal], signal_type: str) -> List[str]:
    """生成带排名和操作指引的信号表格。"""
    lines = []
    if not signals:
        return lines

    # 按有效评分降序排列
    sorted_signals = sorted(signals, key=lambda s: s.effective_score, reverse=True)

    # 根据信号类型选表头
    if signal_type == 'pullback_ma5':
        lines.extend([
            "## 🎯 第一次分歧回踩MA5（策略首选买点）",
            "",
            "> 只做主升中的第一次像样分歧。缩量回踩，不破5日线。",
            "",
            "| # | 股票 | 板块 | 价格 | 涨跌 | 乖离MA5 | 量比 | 换手 | 评分 | 有效分 | 操作要点 | 关键价位 | 确认条件 | 失效条件 | 仓位 |",
            "|---|------|------|------|------|---------|------|------|------|--------|----------|----------|----------|----------|------|",
        ])
    else:
        lines.extend([
            "## ⚠️ 回踩MA10（次优 — 需次日弱转强确认）",
            "",
            "> 已跌破5日线，回踩较深。策略要求不破5日线，此信号仅作参考。",
            "",
            "| # | 股票 | 板块 | 价格 | 涨跌 | 乖离MA5 | 量比 | 换手 | 评分 | 有效分 | 操作要点 | 关键价位 | 确认条件 | 失效条件 | 仓位 |",
            "|---|------|------|------|------|---------|------|------|------|--------|----------|----------|----------|----------|------|",
        ])

    for rank, s in enumerate(sorted_signals, 1):
        guide = _build_action_guide(s)
        rank_str = f"🥇{rank}" if rank == 1 else f"🥈{rank}" if rank == 2 else f"🥉{rank}" if rank == 3 else f"#{rank}"
        key_levels = f"MA5={s.ma5:.2f} MA10={s.ma10:.2f}"

        lines.append(
            f"| {rank_str} | {s.name}({s.code}) | {s.sector} | {s.current_price:.2f} | "
            f"{s.pct_change:+.2f}% | {s.bias_ma5:+.2f}% | "
            f"{s.volume_ratio:.2f} | {s.turnover_rate:.1f}% | "
            f"{s.score} | {s.effective_score} | "
            f"{guide['observation']} | {key_levels} | "
            f"{guide['confirmation']} | {guide['invalidation']} | {guide['sizing']} |"
        )

    lines.append("")
    return lines


def _format_t1_plan(signals: List[TechnicalSignal]) -> List[str]:
    """生成 T+1 操作计划板块。"""
    lines = [
        "---",
        "",
        "## 📋 T+1 操作计划",
        "",
        "> 盘后信号 → 次日观察验证 → 尾盘决定是否介入。以下按信号质量分层展示。",
        "",
    ]

    # 按有效评分分层
    high_priority = [s for s in signals if s.effective_score >= 80]
    medium_priority = [s for s in signals if 60 <= s.effective_score < 80]
    low_priority = [s for s in signals if s.effective_score < 60]

    if high_priority:
        lines.extend([
            "### 🟢 优先关注（有效评分≥80）",
            "",
            "适合尾盘介入。次日确认条件满足即可执行。",
            "",
            "| 排名 | 股票 | 板块 | 评分→有效分 | 介入条件 | 仓位 |",
            "|------|------|------|-------------|----------|------|",
        ])
        for rank, s in enumerate(sorted(high_priority, key=lambda x: x.effective_score, reverse=True), 1):
            guide = _build_action_guide(s)
            lines.append(
                f"| #{rank} | {s.name}({s.code}) | {s.sector} | {s.score}→{s.effective_score} | "
                f"{guide['confirmation']} | {guide['sizing']} |"
            )
        lines.append("")

    if medium_priority:
        lines.extend([
            "### 🟡 备选关注（有效评分60-79）",
            "",
            "需更强确认信号。建议尾盘观察确认后再决定。",
            "",
            "| 排名 | 股票 | 板块 | 评分→有效分 | 介入条件 | 仓位 |",
            "|------|------|------|-------------|----------|------|",
        ])
        for rank, s in enumerate(sorted(medium_priority, key=lambda x: x.effective_score, reverse=True), 1):
            guide = _build_action_guide(s)
            lines.append(
                f"| #{rank} | {s.name}({s.code}) | {s.sector} | {s.score}→{s.effective_score} | "
                f"{guide['confirmation']} | {guide['sizing']} |"
            )
        lines.append("")

    if low_priority:
        lines.extend([
            "### ⚪ 暂不关注（有效评分<60）",
            "",
            "条件不成熟，或市场环境不利。等待后续信号改善。",
            "",
        ])
        for s in sorted(low_priority, key=lambda x: x.effective_score, reverse=True):
            lines.append(f"- {s.name}({s.code}): 评分{s.score}→有效{s.effective_score}，{s.description}")
        lines.append("")

    return lines


def _format_signal_summary(signals: List[TechnicalSignal]) -> List[str]:
    """生成信号汇总（简洁版）。"""
    lines = [
        "---",
        "",
        "## 📈 信号汇总",
        "",
    ]

    # 按有效评分排序
    sorted_sigs = sorted(signals, key=lambda s: s.effective_score, reverse=True)

    for s in sorted_sigs[:15]:
        effective = getattr(s, 'effective_score', s.score)
        if effective >= 80:
            emoji = "🟢"
        elif effective >= 60:
            emoji = "🟡"
        else:
            emoji = "⚪"

        # 构建一句话描述
        desc_parts = [s.description]
        guide = _build_action_guide(s)
        desc_parts.append(f"次日: {guide['observation']}")
        if s.regime_note:
            desc_parts.append(f"[{s.regime_note}]")

        lines.append(f"{emoji} **{s.name}({s.code})** [{s.sector}]: {' | '.join(desc_parts)} | 评分:{s.score}→有效{effective}")

    return lines


def generate_technical_report(
    signals: List[TechnicalSignal],
    removed_stocks: Optional[List[Tuple[str, str, str]]] = None,
    market_env: Optional[Tuple] = None,
    failed_stocks: Optional[List[Tuple[str, str, str]]] = None,
    detail_level: str = "standard",
) -> str:
    """
    生成 Markdown 格式的趋势跟踪日报。

    Args:
        signals: TechnicalSignal 列表
        removed_stocks: (code, name, reason) 元组列表
        market_env: (can_trade, conditions, summary, regime) 或 None
        failed_stocks: (code, name, reason) 元组列表
        detail_level: "compact"（通知精简）| "standard"（文件标准）| "full"（完整含操作计划）

    Returns:
        格式化的 Markdown 字符串
    """
    removed_stocks = removed_stocks or []
    failed_stocks = failed_stocks or []
    today_str = datetime.now().strftime('%Y-%m-%d')

    lines = [
        f"# 📊 趋势跟踪日报 ({today_str})",
        "",
        f"> 共发现 **{len(signals)}** 个技术信号 | 剔除 **{len(removed_stocks)}** 只股票 | 失败 **{len(failed_stocks)}** 只",
        "",
        "---",
        "",
    ]

    # 大盘状态栏
    if market_env:
        can_trade = market_env[0]
        conditions = market_env[1]
        regime = market_env[3] if len(market_env) >= 4 else "chaos"
        regime_text = REGIME_DESC.get(regime, "❓ 状态不明")
        env_icon = "✅" if can_trade else "⛔"
        met = sum(1 for v in conditions.values() if v)
        total = len(conditions)
        lines.extend([
            "## 🌤️ 市场环境",
            "",
            f"> **【大盘状态】{regime_text}**",
            "",
            f"> **{env_icon} {'允许开仓' if can_trade else '建议空仓'}**（满足{met}/{total}项条件）",
            "",
        ])
        for cond_name, met_val in conditions.items():
            icon = "✅" if met_val else ("❌" if met_val is not None else "⟖")
            lines.append(f"- {icon} {cond_name}")

        # 市场环境对信号的影响提示
        regime_modifier = {
            "trending_up": "1.0", "weak_up": "0.85",
            "sideways": "0.8", "trending_down": "0.5",
            "chaos": "0.0",
        }.get(regime, "0.85")
        regime_label = {
            "trending_up": "上行", "weak_up": "弱上行",
            "sideways": "震荡", "trending_down": "下行",
            "chaos": "混沌",
        }.get(regime, "不明")
        lines.extend([
            "",
            f"> 📌 当前 **{regime_label}** 环境下，技术评分已乘以 **×{regime_modifier}** 系数调整为有效评分。",
            "",
            "---",
            "",
        ])

    # 剔除股票
    if removed_stocks:
        lines.extend([
            "## ❌ 剔除股票（趋势破坏）",
            "",
            "| 股票 | 剔除原因 |",
            "|------|----------|",
        ])
        for code, name, reason in removed_stocks[:20]:
            lines.append(f"| {name}({code}) | {reason} |")
        if len(removed_stocks) > 20:
            lines.append(f"| ... | 等共{len(removed_stocks)}只股票 |")
        lines.extend(["", "---", ""])

    # 分析失败
    if failed_stocks:
        lines.extend([
            "## ⚠️ 分析失败股票",
            "",
            "| 股票 | 失败原因 |",
            "|------|----------|",
        ])
        for code, name, reason in failed_stocks[:20]:
            lines.append(f"| {name}({code}) | {reason} |")
        if len(failed_stocks) > 20:
            lines.append(f"| ... | 等共{len(failed_stocks)}只股票 |")
        lines.extend(["", "---", ""])

    # 分类展示
    pullback_ma5_signals = [s for s in signals if s.signal_type == 'pullback_ma5']
    pullback_ma10_signals = [s for s in signals if s.signal_type == 'pullback_ma10']

    # MA5 信号表格（带操作指引）
    lines.extend(_format_rank_table(pullback_ma5_signals, 'pullback_ma5'))

    # MA10 信号表格（带操作指引）
    lines.extend(_format_rank_table(pullback_ma10_signals, 'pullback_ma10'))

    # T+1 操作计划（仅 full 和 standard 模式）
    if detail_level in ("full", "standard"):
        lines.extend(_format_t1_plan(signals))

    # 信号汇总（所有模式都有）
    lines.extend(_format_signal_summary(signals))

    return "\n".join(lines)
