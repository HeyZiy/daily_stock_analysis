# -*- coding: utf-8 -*-
"""
股票剔除规则引擎

四项规则：
1. 连续2天收盘跌破10日线
2. 放量长阴破趋势（单日跌幅>=5% 且 量比>=2）
3. 情绪过热（近5日换手均值 > 近20日均值 * 2，且20日均值>1%避免低基数误触发）
4. 5日平均换手过低且无活跃换手
"""

import logging
from typing import Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def check_removal_rules(code: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """
    检查股票是否应基于四项规则剔除。

    Args:
        code: 股票代码（仅用于日志）
        df: 已排序并计算 MA5/MA10/MA20 的日线 DataFrame

    Returns:
        (是否剔除, 剔除原因)
    """
    if df is None or len(df) < 2:
        return False, ""

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = latest.get('close')
    prev_close = prev.get('close')
    ma10 = latest.get('ma10')
    prev_ma10 = prev.get('ma10')
    volume = latest.get('volume', 0)
    prev_volume = prev.get('volume', 1)
    volume_ratio = volume / prev_volume if prev_volume > 0 else 1

    # 规则1：连续2天收盘跌破10日线
    if (close is not None and ma10 is not None and not pd.isna(ma10) and
        prev_close is not None and prev_ma10 is not None and not pd.isna(prev_ma10)):
        if close < ma10 and prev_close < prev_ma10:
            return True, f"连续2天收盘跌破10日线（昨{prev_close:.2f}<{prev_ma10:.2f}，今{close:.2f}<{ma10:.2f}）"

    # 规则2：放量长阴破趋势（单日放量暴跌，跌幅≥5%且量比≥2）
    if close is not None and prev_close is not None and prev_close > 0:
        pct_change = (close - prev_close) / prev_close * 100
        if (pct_change <= -5 and volume_ratio >= 2 and
            ma10 is not None and not pd.isna(ma10) and
            close < ma10):
            return True, f"放量长阴破趋势（跌幅{pct_change:.1f}%，量比{volume_ratio:.1f}）"

    # 规则3：情绪过热（近5日换手均值 > 近20日均值的2倍）
    if 'turnover_rate' in df.columns and len(df) >= 20:
        r5  = df['turnover_rate'].iloc[-5:].mean()
        r20 = df['turnover_rate'].iloc[-20:].mean()
        if (pd.notna(r5) and pd.notna(r20) and r20 > 1.0 and r5 > r20 * 2.0):
            return True, f"情绪过热（近5日换手{r5:.1f}% 是近20日均{r20:.1f}%的{r5/r20:.1f}倍）"

    # 规则4：5日平均换手过低，且无单日活跃换手
    if 'turnover_rate' in df.columns and len(df) >= 5:
        recent_tr = df['turnover_rate'].iloc[-5:]
        avg_tr = recent_tr.mean()
        if (pd.notna(avg_tr) and avg_tr < 1.0 and (recent_tr >= 3.0).sum() == 0):
            return True, f"5日平均换手{avg_tr:.1f}%过低且无活跃换手"

    return False, ""
