# -*- coding: utf-8 -*-
"""
===================================
数据源策略层 - 包初始化
===================================

本包实现策略模式管理多个数据源，实现：
1. 统一的数据获取接口
2. 自动故障切换
3. 防封禁流控策略

目录结构：
- codes.py         代码/市场判定（纯函数）
- types.py         统一契约（STANDARD_COLUMNS / 异常 / 实时类型）
- bars.py          日线入口（ETF/指数，akshare 单源薄路由）
- realtime.py      实时报价跨源合并
- manager.py       DataFetcherManager（个股日线多源 + 实时合并委托）
- fetchers/        BaseFetcher + 各数据源实现（akshare / efinance / tushare / yfinance / baostock / amazingdata）

访问约定（两层）：
- 统一接口（日线 / 实时报价 / 市场统计）→ 从 data_provider 顶层导入；
- 特性接口（板块名称 / 板块涨跌幅 / 因子 / 基本面等数据源独有能力）→
  直接从 data_provider.fetchers.xxx_fetcher 导入对应 Fetcher（不在统一契约内）。

数据源优先级（动态调整）：
【配置了 TGW 凭证（TGW_APPID + TGW_APP_KEY）+ TUSHARE_TOKEN 时】
1. AmazingDataFetcher (Priority -2) - 最高优先级（星耀数智，需要 TGW 凭证）
2. TushareFetcher (Priority -1) - 次高优先级（动态提升）
3. AkshareFetcher (Priority 0)
4. EfinanceFetcher (Priority 1)
5. BaostockFetcher (Priority 3)
6. YfinanceFetcher (Priority 4)

提示：优先级数字越小越优先，同优先级按初始化顺序排列
"""

from data_provider.fetchers.base import BaseFetcher
from .manager import DataFetcherManager, get_fetcher
from .codes import (
    normalize_stock_code, canonical_stock_code, is_etf_code,
    is_bse_code, is_st_stock, is_kc_cy_stock, ETF_PREFIXES,
)
from .types import (
    STANDARD_COLUMNS, UnifiedRealtimeQuote,
    DataFetchError, RateLimitError, DataSourceUnavailableError,
    RealtimeSource, CircuitBreaker, ChipDistribution,
)
from .bars import get_etf_daily, get_index_daily
from .realtime import merge_realtime_quotes
from data_provider.fetchers.efinance_fetcher import EfinanceFetcher
from data_provider.fetchers.akshare_fetcher import AkshareFetcher, is_hk_stock_code
from data_provider.fetchers.tushare_fetcher import TushareFetcher
from data_provider.fetchers.baostock_fetcher import BaostockFetcher
from data_provider.fetchers.yfinance_fetcher import YfinanceFetcher
from .us_index_mapping import is_us_index_code, is_us_stock_code, get_us_index_yf_symbol, US_INDEX_MAPPING

__all__ = [
    'BaseFetcher',
    'DataFetcherManager',
    'get_fetcher',
    'normalize_stock_code',
    'canonical_stock_code',
    'is_etf_code',
    'is_bse_code',
    'is_st_stock',
    'is_kc_cy_stock',
    'ETF_PREFIXES',
    'STANDARD_COLUMNS',
    'UnifiedRealtimeQuote',
    'DataFetchError',
    'RateLimitError',
    'DataSourceUnavailableError',
    'RealtimeSource',
    'CircuitBreaker',
    'ChipDistribution',
    'get_etf_daily',
    'get_index_daily',
    'merge_realtime_quotes',
    'EfinanceFetcher',
    'AkshareFetcher',
    'is_hk_stock_code',
    'TushareFetcher',
    'BaostockFetcher',
    'YfinanceFetcher',
    'is_us_index_code',
    'is_us_stock_code',
    'get_us_index_yf_symbol',
    'US_INDEX_MAPPING',
]
