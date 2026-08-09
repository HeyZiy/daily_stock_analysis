# -*- coding: utf-8 -*-
"""
===================================
策略规划报告生成器
===================================

将分析结果生成为 Markdown 格式的报告。
"""
from datetime import date
from typing import Dict, List, Any, Optional


def generate_report(analysis: Dict[str, Any], data_text: str = "") -> str:
    """生成 Markdown 报告"""
    today = date.today()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekdays[today.weekday()]

    diagnosis = analysis.get("诊断", {})
    strategy_fit = analysis.get("策略适配", {})
    evolution = analysis.get("策略进化", {})

    lines = []
    lines.append(f"# 📊 策略规划报告")
    lines.append(f"**{today.isoformat()} ({weekday})**")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── 1. 市场诊断 ──
    lines.append("## 一、市场阶段诊断")
    lines.append("")
    if isinstance(diagnosis, dict) and "phase" in diagnosis:
        risk_emoji = {"高": "🔴", "中": "🟡", "低": "🟢"}.get(diagnosis.get("risk_level", ""), "⚪")
        lines.append(f"| 维度 | 判断 |")
        lines.append(f"|------|------|")
        lines.append(f"| **市场阶段** | {diagnosis.get('phase', 'N/A')} |")
        lines.append(f"| **概述** | {diagnosis.get('short_summary', 'N/A')} |")
        lines.append(f"| **趋势分析** | {diagnosis.get('trend_analysis', 'N/A')} |")
        lines.append(f"| **风险等级** | {risk_emoji} {diagnosis.get('risk_level', 'N/A')} |")

        key_signals = diagnosis.get("key_signals", [])
        if key_signals:
            lines.append(f"| **关键信号** | {'<br>'.join(key_signals)} |")

        macro_note = diagnosis.get("macro_note", "")
        if macro_note:
            lines.append(f"| **宏观备注** | {macro_note} |")
    else:
        lines.append(f"> ⚠️ 市场诊断失败: {diagnosis}")
    lines.append("")

    # ── 2. 策略适配 ──
    lines.append("## 二、策略适配度分析")
    lines.append("")
    total_allocation = strategy_fit.get("total_allocation", "")
    if total_allocation:
        lines.append(f"> **总体建议**: {total_allocation}")
        lines.append("")

    assessments = strategy_fit.get("strategy_assessments", [])
    if assessments:
        lines.append("| 策略名称 | 适配度 | 建议权重 | 原因 | 操作指引 |")
        lines.append("|----------|--------|----------|------|----------|")
        for a in assessments:
            fit = a.get("fit_score", 0)
            emoji = "🟢" if fit >= 70 else ("🟡" if fit >= 40 else "🔴")
            lines.append(
                f"| {emoji} {a.get('strategy_name', 'N/A')} "
                f"| {fit}/100 "
                f"| {a.get('suggested_weight', 0)}% "
                f"| {a.get('reason', 'N/A')} "
                f"| {a.get('operation_guide', 'N/A')} |"
            )

    overall_note = strategy_fit.get("overall_note", "")
    if overall_note:
        lines.append("")
        lines.append(f"📝 **本周操作备注**: {overall_note}")

    key_attention = strategy_fit.get("key_attention", [])
    if key_attention:
        lines.append("")
        lines.append("### 本周重点关注")
        for item in key_attention:
            lines.append(f"- 🔍 {item}")
    lines.append("")

    # ── 3. 策略进化 ──
    lines.append("## 三、策略池进化建议")
    lines.append("")
    suggestions = evolution.get("suggestions", [])
    evo_note = evolution.get("evolution_note", "")

    if suggestions:
        lines.append(f"💡 LLM 提议新增 **{len(suggestions)}** 个策略：")
        lines.append("")
        for i, s in enumerate(suggestions, 1):
            lines.append(f"### {i}. {s.get('name', '未命名策略')}（{s.get('category', '未分类')}）")
            lines.append(f"- **描述**: {s.get('description', 'N/A')}")
            lines.append(f"- **适配市场**: {', '.join(s.get('suitable_regimes', []))}")
            lines.append(f"- **为什么现在适合**: {s.get('why_now', 'N/A')}")
            lines.append(f"- **触发条件**: {s.get('core_conditions', 'N/A')}")
            lines.append(f"- **风险提示**: {s.get('risk_note', 'N/A')}")
            lines.append("")
    else:
        lines.append("> 本次分析未提议新策略。")

    if evo_note and evo_note not in ["无", "None", ""]:
        lines.append(f"💬 **改进建议**: {evo_note}")
    lines.append("")

    # ── 4. 数据附录 ──
    lines.append("---")
    lines.append("")
    lines.append("## 附录：本周原始数据")
    lines.append(data_text if data_text else "*无数据*")

    return "\n".join(lines)


def build_bar_chart(assessments: List[Dict[str, Any]]) -> str:
    """根据适配度生成简单的ASCII条形图"""
    if not assessments:
        return ""
    max_name_len = max(len(a.get("strategy_name", "")) for a in assessments)
    lines = ["```"]
    for a in assessments:
        name = a.get("strategy_name", "").ljust(max_name_len)
        score = a.get("fit_score", 0)
        bar = "█" * (score // 5) + "░" * (20 - score // 5)
        emoji = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
        lines.append(f"{name} {emoji} {score:3d} {bar}")
    lines.append("```")
    return "\n".join(lines)
