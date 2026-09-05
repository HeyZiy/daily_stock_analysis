# -*- coding: utf-8 -*-
"""
===================================
数据源策略管理器 DataFetcherManager
===================================

管理一组已实例化的 fetcher（带凭证），提供：
- get_daily_data：个股日线多源（AmazingData > Tushare > akshare > efinance > ...）
- get_realtime_quote：实时报价（委托 realtime.merge_realtime_quotes 跨源合并）
- get_market_stats：市场涨跌统计

ETF/指数日线不归本模块管，见 bars.py。
"""
import logging
import time
from datetime import date, timedelta
from typing import Optional, List, Tuple, Dict, Any

import pandas as pd

from data_provider.fetchers.base import BaseFetcher
from .codes import normalize_stock_code, _is_hk_market
from .types import DataFetchError, summarize_exception
from .realtime import merge_realtime_quotes

logger = logging.getLogger(__name__)


def _clamp_to_last_trading_day(d: date) -> date:
    """把日期收敛到 ≤ d 的最近交易日。

    新鲜度检查的目标日期不能直接用调用方传入的 end_date（常为 date.today()）：
    周末/节假日不是交易日，行情永远不会有当天的 K 线，直接比对会把整条数据源链
    误判为"数据过期"（如 2026-09-05 周六全池失败）。
    日历拉取失败时 trading_calendar 按约定返回空列表 → 退化为周一~周五兜底
    （周末回退到周五，节假日情形宁可放行也不误杀）。
    """
    from src.trading_calendar import get_trading_dates
    trading_days = get_trading_dates(d - timedelta(days=30), d)
    if trading_days:
        return trading_days[-1]
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def get_fetcher():
    """构造 DataFetcherManager 实例（失败返回 None）。供入口层统一调用，避免各模块重复构造。"""
    try:
        return DataFetcherManager()
    except Exception:
        return None



class DataFetcherManager:
    """
    数据源策略管理器
    
    职责：
    1. 管理多个数据源（按优先级排序）
    2. 自动故障切换（Failover）
    3. 提供统一的数据获取接口
    
    切换策略：
    - 优先使用高优先级数据源
    - 失败后自动切换到下一个
    - 所有数据源都失败时抛出异常
    """
    
    def __init__(self, fetchers: Optional[List[BaseFetcher]] = None):
        """
        初始化管理器
        
        Args:
            fetchers: 数据源列表（可选，默认按优先级自动创建）
        """
        self._fetchers: List[BaseFetcher] = []
        
        if fetchers:
            # 按优先级排序
            self._fetchers = sorted(fetchers, key=lambda f: f.priority)
        else:
            # 默认数据源将在首次使用时延迟加载
            self._init_default_fetchers()


    def _init_default_fetchers(self) -> None:
        """
        初始化默认数据源列表

        优先级动态调整逻辑：
        - 如果配置了 TUSHARE_TOKEN：Tushare 优先级提升为 -1（仅次于 AmazingData）
        - 否则按默认优先级：
          -2. AmazingDataFetcher (Priority -2) - 配置了 TGW 凭证时启用（最高）
          -1. TushareFetcher (Priority -1) - 配置了 Token 且初始化成功时（仅次于 AmazingData）
           0. AkshareFetcher (Priority 0)
           1. EfinanceFetcher (Priority 1)
           2. TushareFetcher (Priority 2)
           3. BaostockFetcher (Priority 3)
           4. YfinanceFetcher (Priority 4)
        """
        from data_provider.fetchers.efinance_fetcher import EfinanceFetcher
        from data_provider.fetchers.akshare_fetcher import AkshareFetcher
        from data_provider.fetchers.tushare_fetcher import TushareFetcher
        from data_provider.fetchers.baostock_fetcher import BaostockFetcher
        from data_provider.fetchers.yfinance_fetcher import YfinanceFetcher
        # 创建所有数据源实例（优先级在各 Fetcher 的 __init__ 中确定）
        efinance = EfinanceFetcher()
        akshare = AkshareFetcher()
        tushare = TushareFetcher()  # 会根据 Token 配置自动调整优先级
        baostock = BaostockFetcher()
        yfinance = YfinanceFetcher()

        # 初始化数据源列表
        self._fetchers = [
            efinance,
            akshare,
            tushare,
            baostock,
            yfinance,
        ]

        # 配置了 TGW 凭证时启用 AmazingData（优先数据源）
        try:
            from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher, tgw_configured

            if tgw_configured():
                amazing = AmazingDataFetcher()
                self._fetchers.append(amazing)
                logger.info("已启用 AmazingDataFetcher（星耀数智，TGW 凭证已配置）")
            else:
                logger.debug("未配置 TGW 凭证，跳过 AmazingDataFetcher")
        except Exception as e:
            logger.warning(f"AmazingDataFetcher 初始化失败，已跳过: {e}")

        # 按优先级排序（Tushare 如果配置了 Token 且初始化成功，优先级为 -1，仅次于 AmazingData）
        self._fetchers.sort(key=lambda f: f.priority)

        # 构建优先级说明
        priority_info = ", ".join([f"{f.name}(P{f.priority})" for f in self._fetchers])
        logger.info(f"已初始化 {len(self._fetchers)} 个数据源（按优先级）: {priority_info}")

    
    def get_daily_data(
        self, 
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 30
    ) -> Tuple[pd.DataFrame, str]:
        """
        获取日线数据（自动切换数据源）
        
        故障切换策略：
        1. 美股指数/美股股票直接路由到 YfinanceFetcher
        2. 其他代码从最高优先级数据源开始尝试
        3. 捕获异常后自动切换到下一个
        4. 记录每个数据源的失败原因
        5. 所有数据源失败后抛出详细异常
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            days: 获取天数
            
        Returns:
            Tuple[DataFrame, str]: (数据, 成功的数据源名称)
            
        Raises:
            DataFetchError: 所有数据源都失败时抛出
        """
        from .us_index_mapping import is_us_index_code, is_us_stock_code

        # Normalize code (strip SH/SZ prefix etc.)
        stock_code = normalize_stock_code(stock_code)

        errors = []
        total_fetchers = len(self._fetchers)
        request_start = time.time()

        # 快速路径：美股指数与美股股票直接路由到 YfinanceFetcher
        if is_us_index_code(stock_code) or is_us_stock_code(stock_code):
            for attempt, fetcher in enumerate(self._fetchers, start=1):
                if fetcher.name == "YfinanceFetcher":
                    try:
                        logger.info(
                            f"[数据源尝试 {attempt}/{total_fetchers}] [{fetcher.name}] "
                            f"美股/美股指数 {stock_code} 直接路由..."
                        )
                        df = fetcher.get_daily_data(
                            stock_code=stock_code,
                            start_date=start_date,
                            end_date=end_date,
                            days=days,
                        )
                        if df is not None and not df.empty:
                            elapsed = time.time() - request_start
                            logger.info(
                                f"[数据源完成] {stock_code} 使用 [{fetcher.name}] 获取成功: "
                                f"rows={len(df)}, elapsed={elapsed:.2f}s"
                            )
                            return df, fetcher.name
                    except Exception as e:
                        error_type, error_reason = summarize_exception(e)
                        error_msg = f"[{fetcher.name}] ({error_type}) {error_reason}"
                        logger.warning(
                            f"[数据源失败 {attempt}/{total_fetchers}] [{fetcher.name}] {stock_code}: "
                            f"error_type={error_type}, reason={error_reason}"
                        )
                        errors.append(error_msg)
                    break
            # YfinanceFetcher failed or not found
            error_summary = f"美股/美股指数 {stock_code} 获取失败:\n" + "\n".join(errors)
            elapsed = time.time() - request_start
            logger.error(f"[数据源终止] {stock_code} 获取失败: elapsed={elapsed:.2f}s\n{error_summary}")
            raise DataFetchError(error_summary)

        for attempt, fetcher in enumerate(self._fetchers, start=1):
            try:
                logger.info(f"[数据源尝试 {attempt}/{total_fetchers}] [{fetcher.name}] 获取 {stock_code}...")
                df = fetcher.get_daily_data(
                    stock_code=stock_code,
                    start_date=start_date,
                    end_date=end_date,
                    days=days
                )
                
                if df is not None and not df.empty:
                    # 检查数据新鲜度：最新日期必须 >= 请求截止日对应的最近交易日
                    # （end_date 本身可能是周末/节假日，须先收敛，见 _clamp_to_last_trading_day）
                    try:
                        df_latest = pd.to_datetime(df['date'].max()).date()
                        target_date = pd.to_datetime(end_date).date() if isinstance(end_date, str) else end_date
                        target_date = _clamp_to_last_trading_day(target_date)
                        # 简单判断：如果数据最新日期 < 目标日期，视为过期
                        if df_latest < target_date:
                            raise DataFetchError(
                                f"数据过期(最新:{df_latest}, 需要:{target_date})"
                            )
                    except DataFetchError:
                        raise
                    except Exception as e:
                        # 新鲜度检查出错，记录但继续使用数据
                        logger.debug(f"[{fetcher.name}] 数据新鲜度检查失败: {e}")
                    
                    elapsed = time.time() - request_start
                    logger.info(
                        f"[数据源完成] {stock_code} 使用 [{fetcher.name}] 获取成功: "
                        f"rows={len(df)}, elapsed={elapsed:.2f}s"
                    )
                    # 主源缺失关键列时从备用源补齐；补齐后仍缺失则告警（交由下游降级/跳过）
                    df = self._backfill_missing_columns(
                        df, stock_code, start_date, end_date, days, primary_name=fetcher.name
                    )
                    return df, fetcher.name
                    
            except Exception as e:
                error_type, error_reason = summarize_exception(e)
                error_msg = f"[{fetcher.name}] ({error_type}) {error_reason}"
                logger.warning(
                    f"[数据源失败 {attempt}/{total_fetchers}] [{fetcher.name}] {stock_code}: "
                    f"error_type={error_type}, reason={error_reason}"
                )
                errors.append(error_msg)
                if attempt < total_fetchers:
                    next_fetcher = self._fetchers[attempt]
                    logger.info(f"[数据源切换] {stock_code}: [{fetcher.name}] -> [{next_fetcher.name}]")
                # 继续尝试下一个数据源
                continue
        
        # 所有数据源都失败
        error_summary = f"所有数据源获取 {stock_code} 失败:\n" + "\n".join(errors)
        elapsed = time.time() - request_start
        logger.error(f"[数据源终止] {stock_code} 获取失败: elapsed={elapsed:.2f}s\n{error_summary}")
        raise DataFetchError(error_summary)
    


    
    # 主源未提供、需从备用源补齐的关键列（当前仅换手率）。
    # 各 fetcher 出口已归一化为 STANDARD_COLUMNS，故只按标准列名对齐补齐，
    # 不覆盖主源已有有效数据；补齐后仍整列缺失则告警，交由下游降级/跳过处理。
    # 回退前先按各源的 SUPPORTS_COLUMNS 能力声明过滤，跳过日线接口确定没有该列的源。
    _BACKFILL_COLUMNS = ['turnover_rate']

    def _backfill_missing_columns(
        self,
        primary_df: pd.DataFrame,
        stock_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
        days: int,
        primary_name: str,
    ) -> pd.DataFrame:
        """主源缺失关键列时从其余数据源补齐（按标准化 'date' 列对齐），并告警仍缺失的列。"""
        if primary_df is None or primary_df.empty:
            return primary_df
        for col in self._BACKFILL_COLUMNS:
            if col in primary_df.columns and not primary_df[col].isna().all():
                continue  # 主源已有有效数据，无需补齐
            for fb in self._fetchers:
                if fb.name == primary_name:
                    continue  # 不从主源自身补齐
                if fb.SUPPORTS_COLUMNS is not None and col not in fb.SUPPORTS_COLUMNS:
                    logger.debug(
                        f"[列回退] {stock_code} 跳过 [{fb.name}]：其日线接口不提供 '{col}'"
                    )
                    continue  # 该源确定没有此列，不发无效请求
                try:
                    sub = fb.get_daily_data(
                        stock_code=stock_code,
                        start_date=start_date,
                        end_date=end_date,
                        days=days,
                    )
                except Exception as e:
                    logger.debug(f"[列回退] {stock_code} 从 [{fb.name}] 补齐 '{col}' 失败: {e}")
                    continue
                if sub is None or sub.empty or col not in sub.columns or sub[col].isna().all():
                    continue
                filled = primary_df['date'].map(dict(zip(sub['date'], sub[col])))
                if col not in primary_df.columns:
                    primary_df[col] = filled
                else:
                    missing = primary_df[col].isna()
                    primary_df.loc[missing, col] = filled[missing].values
                logger.info(
                    f"[列回退] {stock_code} 从 [{fb.name}] 补齐缺失列 '{col}' "
                    f"(主源 {primary_name} 未提供)"
                )
                break
            # 遍历所有备用源后仍缺失 → 告警，下游降级/跳过处理
            if col not in primary_df.columns or primary_df[col].isna().all():
                logger.warning(
                    f"[数据缺失] {stock_code} 关键列 '{col}' 在所有数据源均缺失，"
                    f"依赖该列的信号/剔除逻辑将跳过或降级处理"
                )
        return primary_df

    def get_realtime_quote(self, stock_code: str):
        """
        获取实时行情数据（自动故障切换）
        
        故障切换策略（按配置的优先级）：
        1. 美股：使用 YfinanceFetcher.get_realtime_quote()
        2. EfinanceFetcher.get_realtime_quote()
        3. AkshareFetcher.get_realtime_quote(source="em")  - 东财
        4. AkshareFetcher.get_realtime_quote(source="sina") - 新浪
        5. AkshareFetcher.get_realtime_quote(source="tencent") - 腾讯
        6. 返回 None（降级兜底）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            UnifiedRealtimeQuote 对象，所有数据源都失败则返回 None
        """
        # Normalize code (strip SH/SZ prefix etc.)
        stock_code = normalize_stock_code(stock_code)

        from data_provider.fetchers.akshare_fetcher import _is_us_code
        from .us_index_mapping import is_us_index_code
        from src.config import get_config

        config = get_config()

        # 如果实时行情功能被禁用，直接返回 None
        if not config.enable_realtime_quote:
            logger.debug(f"[实时行情] 功能已禁用，跳过 {stock_code}")
            return None

        # 美股指数由 YfinanceFetcher 处理（在美股股票检查之前）
        if is_us_index_code(stock_code):
            return self._quote_from_yfinance(stock_code, "美股指数")

        # 美股单独处理，使用 YfinanceFetcher
        if _is_us_code(stock_code):
            return self._quote_from_yfinance(stock_code, "美股")

        # 港股实时行情只走港股专用入口，避免按 A 股 source_priority
        # 反复触发同一个 ak.stock_hk_spot_em() 接口。
        if _is_hk_market(stock_code):
            fetcher = self._fetcher_by_name("AkshareFetcher")
            if fetcher is not None:
                try:
                    quote = fetcher.get_realtime_quote(stock_code, source="hk")
                    if quote is not None and quote.has_basic_data():
                        logger.info(f"[实时行情] 港股 {stock_code} 成功获取 (来源: akshare_hk)")
                        return quote
                except Exception as e:
                    logger.warning(f"[实时行情] 港股 {stock_code} 获取失败: {e}")
            logger.warning(f"[实时行情] 港股 {stock_code} 无可用数据源")
            return None
        
        # 跨源合并（A 股按 source_priority）抽离为与 manager 解耦的纯函数。
        # 未来剪除日线杂活后，调用方只需持有 fetcher 集合即可直接调用，不再依赖本类。
        source_priority = config.realtime_source_priority.split(',')
        return merge_realtime_quotes(stock_code, self._fetchers, source_priority)

    def _fetcher_by_name(self, name: str) -> Optional["BaseFetcher"]:
        """按类名取 fetcher 实例；_fetchers 延迟加载，故每次现查不缓存。"""
        for fetcher in self._fetchers:
            if fetcher.name == name:
                return fetcher
        return None

    def _quote_from_yfinance(self, stock_code: str, label: str) -> Optional["UnifiedRealtimeQuote"]:
        """美股/美股指数实时行情：仅 YfinanceFetcher 支持。"""
        fetcher = self._fetcher_by_name("YfinanceFetcher")
        if fetcher is not None:
            try:
                quote = fetcher.get_realtime_quote(stock_code)
                if quote is not None:
                    logger.info(f"[实时行情] {label} {stock_code} 成功获取 (来源: yfinance)")
                    return quote
            except Exception as e:
                logger.warning(f"[实时行情] {label} {stock_code} 获取失败: {e}")
        logger.warning(f"[实时行情] {label} {stock_code} 无可用数据源")
        return None

    # Fields worth supplementing from secondary sources when the primary
    # source returns None for them. Ordered by importance.
    _SUPPLEMENT_FIELDS = [
        'volume_ratio', 'turnover_rate',
        'pe_ratio', 'pb_ratio', 'total_mv', 'circ_mv',
        'amplitude',
    ]

    @classmethod
    def _quote_needs_supplement(cls, quote) -> bool:
        """Check if any key supplementary field is still None."""
        for f in cls._SUPPLEMENT_FIELDS:
            if getattr(quote, f, None) is None:
                return True
        return False

    @classmethod
    def _merge_quote_fields(cls, primary, secondary) -> list:
        """
        Copy non-None fields from *secondary* into *primary* where
        *primary* has None. Returns list of field names that were filled.
        """
        filled = []
        for f in cls._SUPPLEMENT_FIELDS:
            if getattr(primary, f, None) is None:
                val = getattr(secondary, f, None)
                if val is not None:
                    setattr(primary, f, val)
                    filled.append(f)
        return filled


    def get_market_stats(self) -> Dict[str, Any]:
        """获取市场涨跌统计（自动切换数据源）"""
        for fetcher in self._fetchers:
            try:
                data = fetcher.get_market_stats()
                if data:
                    logger.info(f"[{fetcher.name}] 获取市场统计成功")
                    return data
            except Exception as e:
                logger.warning(f"[{fetcher.name}] 获取市场统计失败: {e}")
                continue
        return {}


    @staticmethod
    def _has_meaningful_payload(payload: Any) -> bool:
        if payload is None:
            return False
        if isinstance(payload, str):
            normalized = payload.strip().lower()
            return normalized not in ("", "-", "nan", "none", "null", "n/a", "na")
        if isinstance(payload, dict):
            return any(DataFetcherManager._has_meaningful_payload(v) for v in payload.values())
        if isinstance(payload, (list, tuple, set)):
            return any(DataFetcherManager._has_meaningful_payload(v) for v in payload)
        try:
            if pd.isna(payload):
                return False
        except Exception:
            pass
        return True



