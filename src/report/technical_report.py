# -*- coding: utf-8 -*-
"""
趋势跟踪日报 — Markdown 报告生成

接收 TechnicalSignal 和市场环境数据，返回格式化 Markdown 字符串。
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from src.strategy.signal_detector import TechnicalSignal

logger = logging.getLogger(__name__)

REGIME_DESC = {
    "trending_up":   "📈 趋势上行 — 均线多头，适合积极持有缩量回踩信号",
    "sideways":      "➡️ 震荡横盘 — 降低期望，控制仓位，等待方向明确",
    "trending_down": "📉 趋势下行 — 均线空头，建议轻仓观望，持仓注意止损线",
    "unknown":       "❓ 状态不明 — 保守为主，等待方向明确",
}


def generate_technical_report(
    signals: List[TechnicalSignal],
    removed_stocks: Optional[List[Tuple[str, str, str]]] = None,
    market_env: Optional[Tuple] = None,
    failed_stocks: Optional[List[Tuple[str, str, str]]] = None,
) -> str:
    """
    生成 Markdown 格式的趋势跟踪日报。

    Args:
        signals: TechnicalSignal 列表
        removed_stocks: (code, name, reason) 元组列表
        market_env: (can_trade, conditions, summary, regime) 或 None
        failed_stocks: (code, name, reason) 元组列表

    Returns:
        格式化的 Markdown 字符串
    """
    removed_stocks = removed_stocks or []
    failed_stocks = failed_stocks or []
    today_str = datetime.now().strftime('%Y-%m-%d')

    lines = [
        f"# 📊 趋势跟踪日报 ({today_str})",
        "",
        f"> 定位：趋势波段系统。只做主线中的强趋势股，只在分歧回踩时介入。",
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
        regime = market_env[3] if len(market_env) >= 4 else "unknown"
        regime_text = REGIME_DESC.get(regime, "❓ 状态不明")
        env_icon = "✅" if can_trade else "⛔"
        met = sum(1 for v in conditions.values() if v)
        lines.extend([
            "## 🌤️ 市场环境",
            "",
            f"> **【大盘状态】{regime_text}**",
            "",
            f"> **{env_icon} {'允许开仓' if can_trade else '建议空仓'}**（满足{met}/5项条件）",
            "",
        ])
        for cond_name, met_val in conditions.items():
            icon = "✅" if met_val else ("❌" if met_val is not None else "⟖")
            lines.append(f"- {icon} {cond_name}")
        lines.extend(["", "---", ""])

    # 显示剔除的股票
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

    # 第一次分歧回踩MA5（高优先级 — 策略首选买点）
    if pullback_ma5_signals:
        lines.extend([
            "## 🎯 第一次分歧回踩MA5（策略首选买点）",
            "",
            "> 只做主升中的第一次像样分歧。缩量回踩，不破5日线。",
            "",
            "| 股票 | 价格 | MA5 | MA10 | 乖离率 | 量比 | 换手率 | 评分 | 描述 |",
            "|------|------|-----|------|--------|------|--------|------|------|",
        ])
        for s in pullback_ma5_signals:
            lines.append(
                f"| {s.name}({s.code}) | {s.current_price:.2f} | {s.ma5:.2f} | {s.ma10:.2f} | "
                f"{s.bias_ma5:+.2f}% | {s.volume_ratio:.2f} | {s.turnover_rate:.1f}% | {s.score} | {s.description} |"
            )
        lines.append("")

    # 回踩MA10（次优 — 需谨慎，策略要求不破5日线）
    if pullback_ma10_signals:
        lines.extend([
            "## ⚠️ 回踩MA10（次优 — 需次日弱转强确认）",
            "",
            "> 已跌破5日线，回踩较深。策略要求不破5日线，此信号仅作参考。",
            "",
            "| 股票 | 价格 | MA5 | MA10 | 乖离率MA5 | 量比 | 换手率 | 评分 | 描述 |",
            "|------|------|-----|------|-----------|------|--------|------|------|",
        ])
        for s in pullback_ma10_signals:
            lines.append(
                f"| {s.name}({s.code}) | {s.current_price:.2f} | {s.ma5:.2f} | {s.ma10:.2f} | "
                f"{s.bias_ma5:+.2f}% | {s.volume_ratio:.2f} | {s.turnover_rate:.1f}% | {s.score} | {s.description} |"
            )
        lines.append("")

    # 汇总
    lines.extend([
        "---",
        "",
        "## 📈 信号汇总",
        "",
    ])

    for s in signals[:10]:
        emoji = "🟢" if s.score >= 80 else "🟡" if s.score >= 60 else "⚪"
        lines.append(f"{emoji} **{s.name}({s.code})**: {s.description} | 评分:{s.score}")

    lines.extend([
        "",
        "---",
        "",
        "**策略规则**:",
        "- 买点: 主升中的第一次分歧回踩MA5（缩量 + 不破5日线 + 换手率>5%）",
        "- 不做: 加速追高、情绪高潮接力、连续大阳后追涨",
        "- 第一卖点(减仓50%): 放量跌破5日线 / 高位长阴 / 回撤≥5%",
        "- 第二卖点(清仓): 跌破10日线 / 放量跌破10日线",
        "- 环境过滤: 满足2/5项市场条件才允许开仓，否则空仓",
    ])

    return "\n".join(lines)
