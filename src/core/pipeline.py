# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 核心分析流水线
===================================

职责：
1. 管理整个分析流程
2. 协调数据获取、存储、搜索、分析、通知等模块
3. 实现并发控制和异常处理
4. 提供股票分析的核心功能
"""

import logging
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import List, Dict, Any, Optional, Tuple

import pandas as pd

from src.config import get_config, Config
from src.storage import get_db
from data_provider import DataFetcherManager
from data_provider.realtime_types import ChipDistribution
from src.analyzer import GeminiAnalyzer, AnalysisResult, fill_chip_structure_if_needed, fill_price_position_if_needed
from src.data.stock_mapping import STOCK_NAME_MAP
from src.notification import NotificationService, NotificationChannel
from src.search_service import SearchService
from src.services.social_sentiment_service import SocialSentimentService
from src.enums import ReportType
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult
from src.core.trading_calendar import get_market_for_stock, is_market_open
from data_provider.us_index_mapping import is_us_stock_code
from bot.models import BotMessage


logger = logging.getLogger(__name__)


class StockAnalysisPipeline:
    """
    股票分析主流程调度器
    
    职责：
    1. 管理整个分析流程
    2. 协调数据获取、存储、搜索、分析、通知等模块
    3. 实现并发控制和异常处理
    4. 提供股票分析的核心功能
    
    使用示例：
        pipeline = StockAnalysisPipeline(config)
        results = pipeline.run(stock_codes=['600519', '000001'])
    """
    
    def __init__(
        self,
        config: Config,
        max_workers: Optional[int] = None,
        source_message: Optional[BotMessage] = None,
        query_id: Optional[str] = None,
        query_source: Optional[str] = None,
        save_context_snapshot: Optional[bool] = None,
    ):
        """
        初始化分析流水线
        
        Args:
            config: 配置对象
            max_workers: 最大并发数（默认从配置读取）
            source_message: 来源消息（用于Bot上下文）
            query_id: 查询链路关联 id
            query_source: 查询来源（如 "bot"/"web"/"cli"/"system"）
            save_context_snapshot: 是否保存上下文快照（默认从配置读取）
        """
        self.config = config
        self.max_workers = max_workers or config.max_workers
        self.db = get_db()
        self.fetcher_manager = DataFetcherManager(config)
        self.analyzer = GeminiAnalyzer()
        self.notifier = NotificationService()
        self.search_service = SearchService(
            bocha_keys=config.bocha_api_keys,
            tavily_keys=config.tavily_api_keys,
            brave_keys=config.brave_api_keys,
            serpapi_keys=config.serpapi_keys,
            minimax_keys=config.minimax_api_keys,
            searxng_base_urls=config.searxng_base_urls,
            news_max_age_days=config.news_max_age_days,
            news_strategy_profile=getattr(config, "news_strategy_profile", "short"),
        )
        self.trend_analyzer = StockTrendAnalyzer()
        
        # Bot 上下文
        self.source_message = source_message
        self.query_id = query_id or uuid.uuid4().hex
        self.query_source = self._resolve_query_source(query_source)
        
        # 上下文快照保存配置
        if save_context_snapshot is not None:
            self.save_context_snapshot = save_context_snapshot
        else:
            self.save_context_snapshot = getattr(config, 'save_context_snapshot', True)
        
        # 社交舆情服务（仅美股）
        self.social_sentiment_service = SocialSentimentService(
            config=config,
        )
        if self.social_sentiment_service.is_available:
            logger.info("Social sentiment service enabled (Reddit/X/Polymarket, US stocks only)")

        logger.info(f"调度器初始化完成，最大并发数: {self.max_workers}")
        logger.info("已启用趋势分析器 (MA5>MA10>MA20 多头判断)")
        # 打印实时行情/筹码配置状态
        if self.config.enable_realtime_quote:
            logger.info(f"实时行情已启用 (优先级: {self.config.realtime_source_priority})")
        else:
            logger.info("实时行情已禁用，将使用历史收盘价")
        if self.config.enable_chip_distribution:
            logger.info("筹码分布分析已启用")
        else:
            logger.info("筹码分布分析已禁用")
        if self.search_service.is_available:
            logger.info("搜索服务已启用 (Tavily/SerpAPI)")
        else:
            logger.warning("搜索服务未启用（未配置 API Key）")

    def fetch_and_save_stock_data(
        self, 
        code: str,
        force_refresh: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """
        获取并保存单只股票数据
        
        断点续传逻辑：
        1. 检查数据库是否已有今日数据
        2. 如果有且不强制刷新，则跳过网络请求
        3. 否则从数据源获取并保存
        
        Args:
            code: 股票代码
            force_refresh: 是否强制刷新（忽略本地缓存）
            
        Returns:
            Tuple[是否成功, 错误信息]
        """
        try:
            today = date.today()
            # 注意：这里用自然日 date.today() 做"断点续传"判断。
            # 若在周末/节假日/非交易日运行，或机器时区不在中国，可能出现：
            # - 数据库已有最新交易日数据但仍会重复拉取（has_today_data 返回 False）
            # - 或在跨日/时区偏移时误判"今日已有数据"
            # 该行为目前保留（按需求不改逻辑），但如需更严谨可改为"最新交易日/数据源最新日期"判断。

            # 断点续传检查：如果今日数据已存在，跳过
            if not force_refresh and self.db.has_today_data(code, today):
                logger.info(f"[{code}] 今日数据已存在，跳过获取（断点续传）")
                return True, None

            # 从数据源获取数据
            logger.info(f"[{code}] 开始从数据源获取数据...")
            df, source_name = self.fetcher_manager.get_daily_data(code, days=30)

            if df is None or df.empty:
                return False, "获取数据为空"

            # 保存到数据库
            saved_count = self.db.save_daily_data(df, code, source_name)
            logger.info(f"[{code}] 数据保存成功（来源: {source_name}，新增 {saved_count} 条）")

            return True, None

        except Exception as e:
            error_msg = f"获取/保存数据失败: {str(e)}"
            logger.error(f"[{code}] {error_msg}")
            return False, error_msg

    def analyze_stock(self, code: str, report_type: ReportType, query_id: str, tier_info: int = 0) -> Optional[AnalysisResult]:
        """
        分析单只股票（增强版：含量比、换手率、筹码分析、多维度情报）
        
        流程：
        1. 获取实时行情（量比、换手率）- 通过 DataFetcherManager 自动故障切换
        2. 获取筹码分布 - 通过 DataFetcherManager 带熔断保护
        3. 进行趋势分析（基于交易理念）
        4. 多维度情报搜索（最新消息+风险排查+业绩预期）
        5. 从数据库获取分析上下文
        6. 调用 AI 进行综合分析
        
        Args:
            query_id: 查询链路关联 id
            code: 股票代码
            report_type: 报告类型
            
        Returns:
            AnalysisResult 或 None（如果分析失败）
        """
        try:
            # Step 1: 获取实时行情（量比、换手率等）- 使用统一入口，自动故障切换
            # 优先从实时行情获取股票名称，避免重复调用 get_stock_name
            realtime_quote = None
            stock_name = None
            try:
                realtime_quote = self.fetcher_manager.get_realtime_quote(code)
                if realtime_quote:
                    # 优先使用实时行情返回的真实股票名称
                    stock_name = realtime_quote.name
                    # 兼容不同数据源的字段（有些数据源可能没有 volume_ratio）
                    volume_ratio = getattr(realtime_quote, 'volume_ratio', None)
                    turnover_rate = getattr(realtime_quote, 'turnover_rate', None)
                    logger.info(f"{stock_name}({code}) 实时行情: 价格={realtime_quote.price}, "
                              f"量比={volume_ratio}, 换手率={turnover_rate}% "
                              f"(来源: {realtime_quote.source.value if hasattr(realtime_quote, 'source') else 'unknown'})")
                else:
                    logger.info(f"[{code}] 实时行情获取失败或已禁用，将使用历史数据进行分析")
            except Exception as e:
                logger.warning(f"[{code}] 获取实时行情失败: {e}")

            # 如果实时行情没有返回名称，从缓存获取（main.py 已预填充妙想自选股名称）
            if not stock_name:
                stock_name = self.fetcher_manager.get_stock_name(code, allow_realtime=False)

            # 如果还是没有名称，使用代码作为名称
            if not stock_name:
                stock_name = f'股票{code}'

            # Step 2: 获取筹码分布 - 使用统一入口，带熔断保护
            chip_data = None
            try:
                chip_data = self.fetcher_manager.get_chip_distribution(code)
                if chip_data:
                    logger.info(f"{stock_name}({code}) 筹码分布: 获利比例={chip_data.profit_ratio:.1%}, "
                              f"90%集中度={chip_data.concentration_90:.2%}")
                else:
                    logger.debug(f"{stock_name}({code}) 筹码分布获取失败或已禁用")
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 获取筹码分布失败: {e}")

            # If agent mode is explicitly enabled, or specific agent skills are configured, use the Agent analysis pipeline.
            # NOTE: use config.agent_mode (explicit opt-in) instead of
            # config.is_agent_available() so that users who only configured an
            # API Key for the traditional analysis path are not silently
            # switched to Agent mode (which is slower and more expensive).
            use_agent = getattr(self.config, 'agent_mode', False)
            if not use_agent:
                # Auto-enable agent mode when specific skills are configured (e.g., scheduled task with strategy)
                configured_skills = getattr(self.config, 'agent_skills', [])
                if configured_skills and configured_skills != ['all']:
                    use_agent = True
                    logger.info(f"{stock_name}({code}) Auto-enabled agent mode due to configured skills: {configured_skills}")

            # Step 2.5: 基本面能力聚合（统一入口，异常降级）
            fundamental_context = None
            try:
                fundamental_context = self.fetcher_manager.get_fundamental_context(
                    code, realtime_quote=realtime_quote
                )
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 基本面聚合失败: {e}")
                fundamental_context = self.fetcher_manager.build_failed_fundamental_context(code, str(e))

            # 写入基本面快照（如果启用）
            try:
                self.db.save_fundamental_snapshot(
                    code=code,
                    name=stock_name,
                    query_id=query_id,
                    fundamental_data=fundamental_context,
                )
            except Exception as e:
                logger.debug(f"{stock_name}({code}) 基本面快照写入失败: {e}")

            # Step 3: 趋势分析（基于交易理念）— 在 Agent 分支之前执行，供两条路径共用
            trend_result = None
            try:
                df = self.db.get_recent_data(code, days=30)
                if df is not None and not df.empty:
                    # Issue #234: Augment with realtime for intraday MA calculation
                    if self.config.enable_realtime_quote and realtime_quote:
                        df = self._augment_historical_with_realtime(df, realtime_quote, code)
                    trend_result = self.trend_analyzer.analyze(df, code)
                    logger.info(f"{stock_name}({code}) 趋势分析: {trend_result.trend_status.value}, "
                              f"买入信号={trend_result.buy_signal.value}, 评分={trend_result.signal_score}")
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 趋势分析失败: {e}", exc_info=True)

            if use_agent:
                logger.info(f"{stock_name}({code}) 启用 Agent 模式进行分析")
                return self._analyze_with_agent(
                    code,
                    report_type,
                    query_id,
                    stock_name,
                    realtime_quote,
                    chip_data,
                    fundamental_context=fundamental_context,
                    trend_result=trend_result,
                    tier_info=tier_info,
                )

            # Step 4: 多维度情报搜索（最新消息+风险排查+业绩预期）
            news_context = None
            if self.search_service.is_available:
                logger.info(f"{stock_name}({code}) 开始多维度情报搜索...")

                # 使用多维度搜索（最多5次搜索）
                intel_results = self.search_service.search_comprehensive_intel(
                    stock_code=code,
                    stock_name=stock_name,
                    max_searches=5
                )

                # 格式化情报报告
                if intel_results:
                    news_context = self.search_service.format_intel_report(intel_results, stock_name)
                    total_results = sum(
                        len(r.results) for r in intel_results.values() if r.success
                    )
                    logger.info(f"{stock_name}({code}) 情报搜索完成: 共 {total_results} 条结果")
                    logger.debug(f"{stock_name}({code}) 情报搜索结果:\n{news_context}")

                    # 保存新闻情报到数据库（用于后续复盘与查询）
                    try:
                        query_context = self._build_query_context(query_id=query_id)
                        for dim_name, response in intel_results.items():
                            if response and response.success and response.results:
                                self.db.save_news_intel(
                                    code=code,
                                    name=stock_name,
                                    dimension=dim_name,
                                    query=response.query,
                                    response=response,
                                    query_context=query_context
                                )
                    except Exception as e:
                        logger.warning(f"{stock_name}({code}) 保存新闻情报失败: {e}")
            else:
                logger.info(f"{stock_name}({code}) 搜索服务不可用，跳过情报搜索")

            # Step 4.5: Social sentiment intelligence (US stocks only)
            if self.social_sentiment_service.is_available and is_us_stock_code(code):
                try:
                    social_context = self.social_sentiment_service.get_social_context(code)
                    if social_context:
                        logger.info(f"{stock_name}({code}) Social sentiment data retrieved")
                        if news_context:
                            news_context = news_context + "\n\n" + social_context
                        else:
                            news_context = social_context
                except Exception as e:
                    logger.warning(f"{stock_name}({code}) Social sentiment fetch failed: {e}")

            # Step 5: 获取分析上下文（技术面数据）
            context = self.db.get_analysis_context(code)

            if context is None:
                logger.warning(f"{stock_name}({code}) 无法获取历史行情数据，将仅基于新闻和实时行情分析")
                context = {
                    'code': code,
                    'stock_name': stock_name,
                    'date': date.today().isoformat(),
                    'data_missing': True,
                    'today': {},
                    'yesterday': {}
                }
            
            # Step 6: 增强上下文数据（添加实时行情、筹码、趋势分析结果、股票名称）
            enhanced_context = self._enhance_context(
                context, 
                realtime_quote, 
                chip_data,
                trend_result,
                stock_name,  # 传入股票名称
                fundamental_context,
                tier_info=tier_info,
            )
            
            # Step 7: 调用 AI 分析（传入增强的上下文和新闻）
            result = self.analyzer.analyze(enhanced_context, news_context=news_context)

            # Step 7.5: 填充分析时的价格信息到 result
            if result:
                result.query_id = query_id
                realtime_data = enhanced_context.get('realtime', {})
                result.current_price = realtime_data.get('price')
                result.change_pct = realtime_data.get('change_pct')

            # Step 7.6: chip_structure fallback (Issue #589)
            if result and chip_data:
                fill_chip_structure_if_needed(result, chip_data)

            # Step 7.7: price_position fallback
            if result:
                fill_price_position_if_needed(result, trend_result, realtime_quote)

            # Step 8: 保存分析历史记录
            if result:
                try:
                    context_snapshot = self._build_context_snapshot(
                        enhanced_context=enhanced_context,
                        news_content=news_context,
                        realtime_quote=realtime_quote,
                        chip_data=chip_data
                    )
                    self.db.save_analysis_history(
                        result=result,
                        query_id=query_id,
                        report_type=report_type.value,
                        news_content=news_context,
                        context_snapshot=context_snapshot,
                        save_snapshot=self.save_context_snapshot
                    )
                except Exception as e:
                    logger.warning(f"{stock_name}({code}) 保存分析历史失败: {e}")

            return result

        except Exception as e:
            logger.error(f"{stock_name}({code}) 分析失败: {e}")
            logger.exception(f"{stock_name}({code}) 详细错误信息:")
            return None
