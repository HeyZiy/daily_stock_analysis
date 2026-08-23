# -*- coding: utf-8 -*-
"""
===================================
策略规划报告生成器
===================================

将 Agent 1（市场诊断 + 策略提议）与 Agent 2（实现检查）的结果
生成为 Markdown 格式的报告。
"""
from datetime import date
from typing import Dict, List, Any, Optional


def generate_report(analysis: Dict[str, Any], data_text: str = "", todo_summary: str = "") -> str:
    """生成 Markdown 报告"""
    today = date.today()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekdays[today.weekday()]

    diagnosis = analysis.get("诊断", {})
    proposal = analysis.get("策略提议", {})
    impl_results = analysis.get("实现检查", [])

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

    # ── 2. 候选策略（Agent 1） ──
    lines.append("## 二、候选策略与适配度（Agent 1）")
    lines.append("")
    total_allocation = proposal.get("total_allocation", "")
    if total_allocation:
        lines.append(f"> **总体建议**: {total_allocation}")
        lines.append("")

    candidates = proposal.get("candidates", [])
    if candidates:
        lines.append("| 策略名称 | 类别 | 适配度 | 建议权重 | 时间尺度 | 原因 |")
        lines.append("|----------|------|--------|----------|----------|------|")
        recommended_name = proposal.get("recommended", {}).get("name", "")
        for c in candidates:
            fit = c.get("fit_score", 0)
            emoji = "🟢" if fit >= 70 else ("🟡" if fit >= 40 else "🔴")
            star = " ⭐" if c.get("name") == recommended_name else ""
            lines.append(
                f"| {emoji} {c.get('name', 'N/A')}{star} "
                f"| {c.get('category', '未分类')} "
                f"| {fit}/100 "
                f"| {c.get('suggested_weight', 0)}% "
                f"| {c.get('time_scale', 'N/A')} "
                f"| {c.get('reason', 'N/A')} |"
            )

        lines.append("")
        lines.append("### 详细说明")
        for i, c in enumerate(candidates, 1):
            lines.append(f"**{i}. {c.get('name', 'N/A')}**（{c.get('category', '未分类')}）")
            lines.append(f"- 描述: {c.get('description', 'N/A')}")
            lines.append(f"- 操作指引: {c.get('operation_guide', 'N/A')}")
            lines.append("")
    else:
        lines.append("> 策略提议失败或未生成候选。")

    # ── 3. 最推荐策略（Agent 1） ──
    lines.append("## 三、当前最推荐策略")
    lines.append("")
    recommended = proposal.get("recommended", {})
    if recommended and recommended.get("name"):
        lines.append(f"### ⭐ {recommended.get('name', 'N/A')}")
        lines.append("")
        for key, label in [
            ("why", "为什么推荐"),
            ("expected_benefit", "预期收益来源"),
            ("risk_note", "主要风险"),
        ]:
            val = recommended.get(key, "")
            if val:
                lines.append(f"- **{label}**: {val}")
    else:
        lines.append("> 无推荐策略。")
    lines.append("")

    # ── 4. 实现检查（Agent 2） ──
    lines.append("## 四、实现检查（Agent 2）")
    lines.append("")
    if impl_results:
        status_labels = {
            "implemented": "✅ 已有实现",
            "todo_pool": "📋 已加入待办库（池内已登记，缺代码实现）",
            "todo_new": "📋 已加入待办库（全新策略）",
            "error": "⚠️ 池内对账失败，本次未登记",
        }
        for r in impl_results:
            name = r.get("strategy", "")
            status = status_labels.get(r.get("status", ""), "⚪ 跳过")
            lines.append(f"- **{status}**: {name}")
            if r.get("evidence"):
                lines.append(f"  - {r['evidence']}")
            if r.get("match_basis"):
                lines.append(f"  - 匹配依据: {r['match_basis']}")
        lines.append("")
        if todo_summary:
            lines.append("### 策略待办库")
            lines.append("")
            lines.append(todo_summary)
            lines.append("")
    else:
        lines.append("> 未执行实现检查（候选策略为空）。")
        lines.append("")

    overall_note = proposal.get("overall_note", "")
    if overall_note:
        lines.append("---")
        lines.append("")
        lines.append(f"📝 **周度操作备注**: {overall_note}")
        lines.append("")

    key_attention = proposal.get("key_attention", [])
    if key_attention:
        lines.append("")
        lines.append("### 本周重点关注")
        for item in key_attention:
            lines.append(f"- 🔍 {item}")
        lines.append("")

    # ── 5. 数据附录 ──
    lines.append("---")
    lines.append("")
    lines.append("## 附录：本周原始数据")
    lines.append(data_text if data_text else "*无数据*")

    return "\n".join(lines)


def build_bar_chart(candidates: List[Dict[str, Any]]) -> str:
    """根据适配度生成简单的ASCII条形图"""
    if not candidates:
        return ""
    max_name_len = max(len(c.get("name", "")) for c in candidates)
    lines = ["```"]
    for c in candidates:
        name = c.get("name", "").ljust(max_name_len)
        score = c.get("fit_score", 0)
        bar = "█" * (score // 5) + "░" * (20 - score // 5)
        emoji = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
        lines.append(f"{name} {emoji} {score:3d} {bar}")
    lines.append("```")
    return "\n".join(lines)
