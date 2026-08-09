# -*- coding: utf-8 -*-
"""
===================================
numba 指标计算模块（AmazingData 算子）
===================================

将项目内自算的技术指标替换为 AmazingData 的 numba 加速算子：
- MA / EMA / HHV / LLV 等时间序列算子（TimeSeriesFunction）
- 滚动统计算子（StatisticsFunction）

设计：
- 优先使用 AmazingData 算子（numba JIT 编译，性能远高于 pandas rolling）
- AmazingData 未安装或导入失败时自动回退到 pandas 实现（保持 CI 可用）
- 所有函数入参/出参均为 pandas Series，与旧逻辑兼容
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# === AmazingData 算子懒加载 ===
_tsf = None
_sf = None
_tsf_load_error: Optional[str] = None


def _load_operators():
    """
    懒加载 AmazingData 算子模块。

    Returns:
        (TimeSeriesFunction, StatisticsFunction) 或 (None, None)
    """
    global _tsf, _sf, _tsf_load_error
    if _tsf is not None and _sf is not None:
        return _tsf, _sf
    if _tsf_load_error:
        return None, None
    try:
        from AmazingData.operator.time_series_function import TimeSeriesFunction
        from AmazingData.operator.statistics_function import StatisticsFunction

        _tsf = TimeSeriesFunction
        _sf = StatisticsFunction
        logger.info("AmazingData numba 算子加载成功")
    except Exception as e:
        _tsf_load_error = str(e)
        logger.warning(f"AmazingData 算子不可用，回退 pandas 计算: {e}")
        return None, None
    return _tsf, _sf


def _to_series(result, index: pd.Index) -> pd.Series:
    """将算子返回值包装为与入参同索引的 Series。"""
    if result is None:
        return pd.Series([float('nan')] * len(index), index=index)
    if isinstance(result, pd.Series):
        return result
    return pd.Series(result, index=index)


def ma(series: pd.Series, n: int, min_periods: int = 1) -> pd.Series:
    """简单移动平均，与 pandas rolling(n, min_periods).mean() 语义一致。"""
    tsf, _ = _load_operators()
    if tsf is None:
        return series.rolling(window=n, min_periods=min_periods).mean()
    result = _to_series(tsf.MA(series, n), series.index)
    if min_periods > 1:
        # numba 算子不足窗口也会输出部分均值，这里按 min_periods 语义置 NaN
        result = result.where(pd.Series(range(len(series)), index=series.index) >= min_periods - 1)
    return result


def ema(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均（alpha=2/(n+1)），与 pandas ewm(span=n, adjust=False) 语义一致。"""
    tsf, _ = _load_operators()
    if tsf is None:
        return series.ewm(span=n, adjust=False).mean()
    return _to_series(tsf.EMA(series, n), series.index)


def hhv(series: pd.Series, n: int) -> pd.Series:
    """N 周期最高值。"""
    tsf, _ = _load_operators()
    if tsf is None:
        return series.rolling(window=n, min_periods=1).max()
    return _to_series(tsf.HHV(series, n), series.index)


def llv(series: pd.Series, n: int) -> pd.Series:
    """N 周期最低值。"""
    tsf, _ = _load_operators()
    if tsf is None:
        return series.rolling(window=n, min_periods=1).min()
    return _to_series(tsf.LLV(series, n), series.index)


def rolling_mean_shifted(series: pd.Series, n: int, shift: int = 1) -> pd.Series:
    """N 周期均值再平移 shift 位（用于量比等需要前值均值的场景）。"""
    m = ma(series, n, min_periods=1)
    return m.shift(shift)


def beta(series_x: pd.Series, series_b: pd.Series, n: int) -> pd.Series:
    """滚动 Beta 系数。"""
    _, sf = _load_operators()
    if sf is None:
        x = series_x.rolling(window=n, min_periods=n).cov(series_b)
        v = series_b.rolling(window=n, min_periods=n).var()
        return x / v
    return _to_series(sf.BETA(series_x, series_b, n), series_x.index)


def rolling_std(series: pd.Series, n: int) -> pd.Series:
    """滚动标准差（ddof=0 口径，与 AmazingData STD 一致）。"""
    _, sf = _load_operators()
    if sf is None:
        return series.rolling(window=n, min_periods=1).std(ddof=0)
    return _to_series(sf.STD(series, n), series.index)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """
    计算 MACD 指标。

    Returns:
        DataFrame: MACD_DIF, MACD_DEA, MACD_BAR
    """
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    bar = (dif - dea) * 2
    return pd.DataFrame({'MACD_DIF': dif, 'MACD_DEA': dea, 'MACD_BAR': bar})


def rsi(series: pd.Series, period: int) -> pd.Series:
    """
    计算 RSI 指标（简单均值口径，与 analyzer.py 原逻辑完全一致）。

    RSI = 100 - (100 / (1 + RS))，RS = 平均上涨 / 平均下跌
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = ma(gain, period, min_periods=period)
    avg_loss = ma(loss, period, min_periods=period)
    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)


def volume_ratio(volume: pd.Series) -> pd.Series:
    """
    量比：当日成交量 / 前 5 日均量。

    与 BaseFetcher._calculate_indicators 的旧逻辑一致：
    当日量 / 5日均量(shift 1)，无前值补 1.0。
    """
    avg_volume_5 = ma(volume, 5, min_periods=1)
    ratio = volume / avg_volume_5.shift(1)
    return ratio.fillna(1.0)
