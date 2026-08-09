# -*- coding: utf-8 -*-
"""
===================================
LLM 分析器 — 市场诊断 + 策略适配 + 策略进化
===================================

核心流程：
1. 输入：周度市场数据 + 策略注册表
2. Phase 1: 市场阶段诊断
3. Phase 2: 逐策略适配度分析
4. Phase 3: 策略池进化建议
"""
import json
import logging
from datetime import date
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

MARKET_DIAGNOSIS_PROMPT = """你是一个专业的多策略量化投资顾问。请根据以下周度市场数据，诊断当前A股市场所处的阶段。

## 诊断要求
请从以下几个维度分析：
1. **主要趋势方向**：宽基指数的中短期趋势（周线/月线级别）
2. **市场结构**：大盘 vs 小盘、价值 vs 成长、行业分化程度
3. **宏观环境**：流动性、风险偏好、外部环境
4. **情绪位置**：贪婪还是恐慌？是否极端？
5. **阶段判断**：当前处于牛/熊/震荡的哪个子阶段（初期/中期/末期）

## 输出格式（JSON）
{
  "phase": "牛/熊/震荡的某个子阶段，如'牛市中期'、'震荡市偏弱'等",
  "short_summary": "一句话描述当前市场状态（20字以内）",
  "trend_analysis": "趋势方向分析（100字以内）",
  "risk_level": "高/中/低",
  "key_signals": ["关键信号1", "关键信号2", "关键信号3"],
  "macro_note": "宏观环境备注（50字以内）"
}

只输出JSON，不要其他内容。"""


STRATEGY_FIT_PROMPT = """你是一个多策略量化投资顾问。当前市场已诊断为：

**市场阶段**: {phase}
**风险等级**: {risk_level}
**趋势分析**: {trend_analysis}
**关键信号**: {key_signals}

以下是策略池中的全部策略：

{strategies_text}

## 任务
逐策略分析在当前市场环境下的适配度，并给出权重建议。

## 分析要求
对每个策略给出：
- **适配度**（0-100分）：100=非常适合，0=完全不适合
- **适配度理由**（30字以内）：为什么适合/不适合当前市场
- **建议权重**（%）：如果总分100%，这个策略应该占多少
- **操作指引**：如果该策略有现成实现，应该怎么调参数

所有策略的权重之和必须为100%。

## 输出格式（JSON）
{{
  "total_allocation": "总体资产配置建议（如：70%仓位运行/30%现金观望）",
  "strategy_assessments": [
    {{
      "strategy_id": "策略ID",
      "strategy_name": "策略名称",
      "fit_score": 85,
      "reason": "适配原因（30字内）",
      "suggested_weight": 25,
      "operation_guide": "操作指引（如：扩大筛选范围/降低仓位等）"
    }}
  ],
  "overall_note": "周度整体操作备注（100字以内）",
  "key_attention": ["本周关注点1", "本周关注点2", "本周关注点3"]
}}

只输出JSON，不要其他内容。"""


STRATEGY_EVOLUTION_PROMPT = """你是一个策略研究专家。根据当前市场环境分析，考虑是否有新的策略值得加入策略池。

## 当前市场阶段
{phase_summary}

## 当前策略池
{strategies_text}

## 任务
思考：在当前市场环境下，是否存在现有策略池**未覆盖**的策略类型？

如果有，请描述新策略并以JSON格式输出。如果没有，返回空的 suggestions 数组。

新策略要求：
- 与现有策略有明显差异（不是简单变体）
- 在当前市场阶段有实际应用价值
- 有明确的适用条件和退出条件

## 输出格式（JSON）
{{
  "suggestions": [
    {{
      "name": "策略名称",
      "category": "策略类别（趋势跟踪/资产配置/防御策略/震荡策略/反转策略/对冲策略/动量策略/事件驱动/其他）",
      "description": "策略详细描述（50-100字）",
      "suitable_regimes": ["适配的市场状态"],
      "why_now": "为什么当前市场适合这个策略（30字内）",
      "core_conditions": "核心触发条件",
      "risk_note": "主要风险提示"
    }}
  ],
  "evolution_note": "对当前策略池的改进建议（50字内，没有则写'无'）"
}}

只输出JSON，不要其他内容。"""


def _call_llm(prompt: str, data_text: str, temperature: float = 0.3) -> str:
    from src.strategy_planner.llm_client import get_llm_client
    client = get_llm_client()
    if not client.available:
        return '{"error": "LLM 不可用"}'
    full_prompt = data_text + "\n\n---\n\n" + prompt
    return client.chat("你是一个专业的量化投资策略分析助手。请严格按照要求的JSON格式输出。", full_prompt, temperature)


def run_market_diagnosis(market_data_text: str) -> Dict[str, Any]:
    """Phase 1: 市场阶段诊断"""
    logger.info("Phase 1: 开始市场阶段诊断...")
    raw = _call_llm(MARKET_DIAGNOSIS_PROMPT, market_data_text)
    try:
        result = json.loads(raw)
        logger.info(f"市场诊断完成: {result.get('phase', '未知')}")
        return result
    except json.JSONDecodeError:
        logger.warning(f"LLM 返回非JSON格式，尝试修复: {raw[:200]}...")
        # 尝试提取 JSON
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"phase": "无法判断", "short_summary": "LLM输出解析失败", "risk_level": "未知", "error": raw[:500]}


def run_strategy_fit(market_data_text: str, diagnosis: Dict[str, Any], strategies_text: str) -> Dict[str, Any]:
    """Phase 2: 策略适配度分析"""
    logger.info("Phase 2: 开始策略适配度分析...")
    prompt = STRATEGY_FIT_PROMPT.format(
        phase=diagnosis.get("phase", "未知"),
        risk_level=diagnosis.get("risk_level", "中"),
        trend_analysis=diagnosis.get("trend_analysis", "未知"),
        key_signals=", ".join(diagnosis.get("key_signals", [])),
        strategies_text=strategies_text,
    )
    raw = _call_llm(prompt, market_data_text, temperature=0.4)
    try:
        result = json.loads(raw)
        logger.info(f"策略适配分析完成: {len(result.get('strategy_assessments', []))}个策略已评估")
        return result
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"strategy_assessments": [], "overall_note": "分析失败", "key_attention": []}


def run_strategy_evolution(diagnosis: Dict[str, Any], strategies_text: str) -> Dict[str, Any]:
    """Phase 3: 策略池进化建议"""
    logger.info("Phase 3: 开始策略进化分析...")
    prompt = STRATEGY_EVOLUTION_PROMPT.format(
        phase_summary=f"{diagnosis.get('phase', '未知')} | 风险{diagnosis.get('risk_level', '中')} | {diagnosis.get('short_summary', '')}",
        strategies_text=strategies_text,
    )
    raw = _call_llm(prompt, "", temperature=0.6)
    try:
        result = json.loads(raw)
        suggestions = result.get("suggestions", [])
        logger.info(f"策略进化分析完成: {len(suggestions)}个新策略提议")
        return result
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"suggestions": [], "evolution_note": "分析失败"}


def run_full_analysis(data_text: str, strategies_text: str) -> Dict[str, Any]:
    """运行完整的三阶段分析"""
    result = {
        "诊断": {},
        "策略适配": {},
        "策略进化": {},
        "时间": date.today().isoformat(),
    }

    # Phase 1
    result["诊断"] = run_market_diagnosis(data_text)

    # Phase 2
    result["策略适配"] = run_strategy_fit(data_text, result["诊断"], strategies_text)

    # Phase 3
    result["策略进化"] = run_strategy_evolution(result["诊断"], strategies_text)

    # 处理进化建议：加入待审批列表
    suggestions = result["策略进化"].get("suggestions", [])
    if suggestions:
        from src.strategy_planner.strategy_registry import add_pending_strategy
        for s in suggestions:
            add_pending_strategy(s)

    return result
