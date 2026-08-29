# -*- coding: utf-8 -*-
"""
===================================
A 股交易日历（公共模块）
===================================

提供交易日判断与区间查询，供各脚本（trend_analysis / etf_observe / style_report 等）复用。

数据来源：akshare `tool_trade_date_hist_sina()`（新浪交易日历）。
设计：
- 模块级缓存，进程内只拉一次
- 拉取失败时按交易日放行（避免节假日误杀，宁可多跑也不漏跑）
"""

import logging
from datetime import date, datetime
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_trading_dates: List[date] = []
_loaded = False


def _ensure_loaded() -> None:
    """惰性加载交易日历到模块级缓存（幂等）"""
    global _trading_dates, _loaded
    if _loaded:
        return
    try:
        import akshare as ak
        cal_df = ak.tool_trade_date_hist_sina()
        _trading_dates = sorted(
            pd.to_datetime(row["trade_date"]).date()
            for _, row in cal_df.iterrows()
        )
        _loaded = True
        logger.info(f"交易日历已加载：{len(_trading_dates)} 个交易日")
    except Exception as e:
        logger.warning(f"获取交易日历失败（按交易日放行）: {e}")
        _loaded = True  # 标记已尝试，避免重复失败


def get_trading_dates(start_date: date, end_date: date) -> List[date]:
    """返回 [start_date, end_date] 区间内的交易日列表（升序）。"""
    _ensure_loaded()
    if not _trading_dates:
        return []
    return [d for d in _trading_dates if start_date <= d <= end_date]


def is_trading_day(day: Optional[date] = None) -> bool:
    """判断某天是否为 A 股交易日（默认今天）。

    Returns:
        True 表示交易日；日历拉取失败时按交易日放行。
    """
    _ensure_loaded()
    if not _trading_dates:
        return True
    return (day or datetime.now().date()) in _trading_dates
