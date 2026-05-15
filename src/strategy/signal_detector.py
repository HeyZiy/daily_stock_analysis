# -*- coding: utf-8 -*-
"""
趋势波段策略 — 分歧回踩信号检测

核心买点逻辑：
- 第一次分歧回踩 MA5（缩量 + 不破5日线 + 换手率>5%）
- 回踩 MA10（次优，需确认）
"""

import logging
from dataclasses import dataclass
from typing import List

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class TechnicalSignal:
    """技术信号数据类"""
    code: str
    name: str
    signal_type: str  # 'pullback_ma5', 'pullback_ma10' 等
    score: int  # 0-100
    current_price: float
    ma5: float
    ma10: float
    ma20: float
    bias_ma5: float  # 乖离率
    volume_ratio: float  # 量比
    turnover_rate: float  # 换手率
    description: str  # 信号描述


def detect_pullback_signals(code: str, name: str, df: pd.DataFrame) -> List[TechnicalSignal]:
    """
    检测缩量回踩 MA5 / MA10 趋势波段信号。

    Args:
        code: 股票代码
        name: 股票名称
        df: 已计算 MA5/MA10/MA20 的日线 DataFrame

    Returns:
        匹配规则的 TechnicalSignal 列表
    """
    signals: List[TechnicalSignal] = []

    if df is None:
        logger.warning(f"⚠️ {name}({code}): 数据为空，跳过分析")
        return signals

    if len(df) < 20:
        logger.warning(f"⚠️ {name}({code}): 数据不足20条(仅{len(df)}条)，可能影响分析准确性")

    df = df.sort_values('date').reset_index(drop=True)

    # 取最新数据
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None

    current_price = latest['close']
    ma5 = latest['ma5']
    ma10 = latest['ma10']
    ma20 = latest['ma20']
    _vr = latest.get('volume_ratio')
    volume_ratio = float(_vr) if _vr is not None and pd.notna(_vr) else 1.0

    if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
        return signals

    # 计算乖离率
    bias_ma5 = (current_price - ma5) / ma5 * 100 if ma5 > 0 else 0

    # 计算当日涨跌幅（用于描述，不再作为硬条件）
    pct_change = 0.0
    if prev is not None and prev['close'] > 0:
        pct_change = (current_price - prev['close']) / prev['close'] * 100

    # 计算日内承接：通过上下影线和收盘位置判断资金承接意愿
    intraday_range = latest['high'] - latest['low']
    if intraday_range > 0:
        close_position = (latest['close'] - latest['low']) / intraday_range
        upper_shadow_ratio = (latest['high'] - max(latest['open'], latest['close'])) / intraday_range
        lower_shadow_ratio = (min(latest['open'], latest['close']) - latest['low']) / intraday_range
        has_intraday_support = (
            close_position > 0.7
            and upper_shadow_ratio < 0.35
            and lower_shadow_ratio > 0.2
        )
    else:
        close_position = 0.5
        has_intraday_support = True

    # === 策略检查 ===

    # 1. 均线多头排列：5日线 > 10日线 > 20日线
    is_bullish_alignment = ma5 > ma10 > ma20

    # 2. 不破5日线（或盘中破但尾盘收回）
    holds_ma5 = current_price >= ma5 * 0.995  # 允许微破

    # 3. 选股池条件：换手率 > 5%（保证活跃度）
    turnover = latest.get('turnover_rate', 0)
    meets_liquidity = turnover > 5.0

    # 4. 非情绪过热检查：排除短期涨幅过大、偏离5日线过远、波动剧烈的标的
    is_euphoric = False
    if len(df) >= 4:
        recent_3d_gain = (df.iloc[-1]['close'] - df.iloc[-4]['close']) / df.iloc[-4]['close'] * 100
        recent_5d_gain = (df.iloc[-1]['close'] - df.iloc[-6]['close']) / df.iloc[-6]['close'] * 100 if len(df) >= 6 else 0
        recent_max_bias_ma5 = max(
            (df.iloc[i]['close'] - df.iloc[i]['ma5']) / df.iloc[i]['ma5'] * 100
            for i in range(-min(5, len(df)), 0)
            if pd.notna(df.iloc[i]['ma5']) and df.iloc[i]['ma5'] > 0
        ) if len(df) >= 2 else 0
        # 近3日最大振幅（高波动 = 博弈激烈，不适合低吸）
        recent_max_amplitude = max(
            (df.iloc[i]['high'] - df.iloc[i]['low']) / df.iloc[i-1]['close'] * 100
            for i in range(-3, 0)
            if df.iloc[i-1]['close'] > 0
        )
        if recent_3d_gain >= 18 or recent_5d_gain >= 30 or recent_max_bias_ma5 >= 12 or recent_max_amplitude >= 15:
            is_euphoric = True

    # 5. 量能检查：当日成交量 < 5日均量 * 1.1（不允许爆量，而非必须地量）
    current_volume = latest['volume']
    volume_ma5 = df['volume'].rolling(5).mean().iloc[-1] if len(df) >= 5 else 0
    no_volume_blowoff = volume_ma5 > 0 and current_volume < volume_ma5 * 1.1

    # 6. 第一次回踩MA5检查：过去5天收盘价始终在MA5之上
    recently_above_ma5 = (
        all(df.iloc[i]['close'] > df.iloc[i]['ma5'] for i in range(-6, -1))
        if len(df) >= 6 else False
    )

    # 7. MA10 回踩确认：盘中最低价接近MA10且收盘守住
    touches_ma10 = latest['low'] <= ma10 * 1.01 and current_price >= ma10

    # 只有均线多头排列的股票才有分析意义
    if not is_bullish_alignment:
        return signals

    # 信号1: 缩量回踩 MA5（主升中的第一次分歧回踩）— 最佳买点
    # 条件：多头排列 + 守住MA5 + 缩量 + 小实体/小跌 + 换手达标 + 非加速
    if (holds_ma5 and no_volume_blowoff and abs(bias_ma5) < 2.0
            and has_intraday_support and meets_liquidity
            and not is_euphoric and recently_above_ma5):

        # --- 龙头换手标注（借鉴 dragon_head 策略）---
        signal_desc = f"第一次分歧回踩MA5，缩量（量比{volume_ratio:.2f}）不破5日线，涨跌{pct_change:+.2f}%"
        if turnover > 8.0 and 2.0 <= pct_change <= 5.0:
            signal_desc += " ⭐换手活跃具龙头特征"
        elif turnover > 5.0:
            signal_desc += f" 换手{turnover:.1f}%正常"

        # --- 一阳三阴形态标注（借鉴 one_yang_three_yin 策略）---
        if len(df) >= 4:
            anchor = df.iloc[-4]
            anchor_pct = (anchor['close'] - anchor['open']) / anchor['open'] * 100 if anchor['open'] > 0 else 0
            pullback_days = df.iloc[-3:]
            if anchor_pct > 3.0 and all(row['close'] < row['open'] for _, row in pullback_days.iterrows()):
                # 量确认：后三天量递减，且平均量 < 阳线量
                pullback_volumes = df.iloc[-3:]['volume']
                if (pullback_volumes.is_monotonic_decreasing
                        and pullback_volumes.mean() < anchor['volume']):
                    signal_desc += " 📐一阳三阴形态"

        signals.append(TechnicalSignal(
            code=code,
            name=name,
            signal_type='pullback_ma5',
            score=90,
            current_price=current_price,
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            bias_ma5=bias_ma5,
            volume_ratio=volume_ratio,
            turnover_rate=turnover,
            description=signal_desc
        ))

    # 信号2: 缩量回踩 MA10（次优买点 — 回踩较深，需确认支撑）
    # 策略强调"不破5日线"，回踩MA10说明分歧较大，评分降低
    elif (no_volume_blowoff
          and touches_ma10  # 盘中回踩MA10且收盘守住
          and has_intraday_support
          and meets_liquidity
          and not is_euphoric
          and not holds_ma5):  # 确实跌破了MA5
        signals.append(TechnicalSignal(
            code=code,
            name=name,
            signal_type='pullback_ma10',
            score=65,  # 策略偏谨慎，回踩MA10评分降低
            current_price=current_price,
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            bias_ma5=bias_ma5,
            volume_ratio=volume_ratio,
            turnover_rate=turnover,
            description=f"回踩MA10（回踩较深），缩量（量比{volume_ratio:.2f}），涨跌{pct_change:+.2f}%，需次日弱转强确认"
        ))

    # 注意：不放量突破信号 — 策略明确规定"不做加速追高"

    return signals
