# -*- coding: utf-8 -*-
"""
===================================
实时行情跨源合并
===================================

（量比 / 换手率 / 估值等），需要跨多个已实例化的 fetcher 合并补齐。

设计边界：
- fetcher 实例集合（带凭证）由调用方持有 / 传入 —— 它是 data_provider
  的真实资产，不属于本模块。本模块只负责"给定 fetcher 集合 + 源优先级
  → 合并报价"这一纯算法。
- 本模块不处理美股 / 港股 / 美股指数的特殊路由（那些依赖 manager 的
  _fetcher_by_name 取特定 fetcher），只处理 A 股按 source_priority 的合并。

用法：持有 fetcher 集合的调用方（manager.get_realtime_quote 等）直接
调用 merge_realtime_quotes(code, fetchers, source_priority)。
"""
import logging
from typing import Any, Dict, List, Optional

from .types import UnifiedRealtimeQuote

logger = logging.getLogger(__name__)


# 关键补充字段（按重要性）。主源缺失则尝试从后续源补齐。
_SUPPLEMENT_FIELDS = [
    "volume_ratio", "turnover_rate",
    "pe_ratio", "pb_ratio", "total_mv", "circ_mv",
    "amplitude",
]

# source 名 → (fetcher 类名, 调用参数)。须与 manager.get_realtime_quote 保持一致。
_SOURCE_SPECS: Dict[str, tuple] = {
    "efinance": ("EfinanceFetcher", {}),
    "akshare_em": ("AkshareFetcher", {"source": "em"}),
    "akshare_sina": ("AkshareFetcher", {"source": "sina"}),
    "tencent": ("AkshareFetcher", {"source": "tencent"}),
    "akshare_qq": ("AkshareFetcher", {"source": "tencent"}),  # tencent 别名
    "tushare": ("TushareFetcher", {}),
}


def _quote_needs_supplement(quote: UnifiedRealtimeQuote) -> bool:
    """任何关键补充字段仍为 None 即视为需要补充。"""
    return any(getattr(quote, f, None) is None for f in _SUPPLEMENT_FIELDS)


def _merge_quote_fields(primary: UnifiedRealtimeQuote,
                        secondary: UnifiedRealtimeQuote) -> List[str]:
    """将 secondary 的非 None 补充字段写入 primary（仅补 primary 缺失项）。

    Returns:
        被填入的字段名列表（用于日志）。
    """
    filled = []
    for f in _SUPPLEMENT_FIELDS:
        if getattr(primary, f, None) is None:
            val = getattr(secondary, f, None)
            if val is not None:
                setattr(primary, f, val)
                filled.append(f)
    return filled


def merge_realtime_quotes(
    stock_code: str,
    fetchers: List[Any],
    source_priority: List[str],
) -> Optional[UnifiedRealtimeQuote]:
    """跨源合并实时行情。

    Args:
        stock_code: 已规范化的 6 位代码（调用方负责 normalize_stock_code）。
        fetchers: 已实例化的 fetcher 列表（按 .name 属性匹配 _SOURCE_SPECS）。
        source_priority: 源优先级列表，如
            ["efinance", "akshare_em", "akshare_sina", "tencent", "tushare"]。

    Returns:
        合并后的 UnifiedRealtimeQuote：首个有效源为 primary，后续源最多补充一次
        缺失字段；全部失败返回 None。
    """
    fetcher_map = {f.name: f for f in fetchers}
    primary_quote = None
    supplement_attempts = 0
    errors = []

    for source in source_priority:
        source = source.strip().lower()
        spec = _SOURCE_SPECS.get(source)
        if spec is None:
            logger.debug(f"[实时合并] {stock_code} 未知数据源 '{source}'，跳过")
            continue
        fetcher = fetcher_map.get(spec[0])
        if fetcher is None:
            logger.debug(f"[实时合并] {stock_code} 数据源 '{source}' 依赖的 {spec[0]} 未启用，跳过")
            continue
        try:
            quote = fetcher.get_realtime_quote(stock_code, **spec[1])
        except Exception as e:
            logger.warning(f"[实时合并] {stock_code} [{source}] 失败: {e}")
            errors.append(f"[{source}] {e}")
            continue

        if quote is None or not quote.has_basic_data():
            continue

        if primary_quote is None:
            primary_quote = quote
            logger.info(f"[实时合并] {stock_code} 成功获取 (来源: {source})")
            if not _quote_needs_supplement(primary_quote):
                return primary_quote
            logger.debug(f"[实时合并] {stock_code} 部分字段缺失，尝试从后续数据源补充")
        else:
            # 补充缺失字段（限制一次补充尝试，避免无谓请求）
            supplement_attempts += 1
            if supplement_attempts > 1:
                logger.debug(f"[实时合并] {stock_code} 补充尝试已达上限，停止继续")
                break
            filled = _merge_quote_fields(primary_quote, quote)
            if filled:
                logger.info(f"[实时合并] {stock_code} 从 {source} 补充了缺失字段: {filled}")
            if not _quote_needs_supplement(primary_quote):
                break

    if primary_quote is not None:
        return primary_quote

    if errors:
        logger.warning(f"[实时合并] {stock_code} 所有数据源均失败，降级处理: {'; '.join(errors)}")
    else:
        logger.warning(f"[实时合并] {stock_code} 无可用数据源")
    return None
