# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 大盘复盘独立入口
===================================

职责：
1. 提供独立 CLI 入口运行大盘复盘
2. 封装组件初始化与交易日过滤逻辑
3. 可被导入为模块使用，也可直接运行

使用方式：
    python market_review.py              # 正常运行
    python market_review.py --region us  # 美股大盘
    python market_review.py --no-notify  # 不推送通知
    python market_review.py --force-run  # 跳过交易日检查
"""

import sys
import argparse
import logging

from src.config import setup_env, get_config, Config
from src.logging_config import setup_logging

setup_env()

from src.analyzer import GeminiAnalyzer
from src.notification import NotificationService
from src.search_service import SearchService
from src.core.market_review import run_market_review
from src.core.trading_calendar import get_open_markets_today, compute_effective_region


def parse_args():
    parser = argparse.ArgumentParser(
        description='大盘复盘独立入口',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python market_review.py                  # 默认地区，正常运行
  python market_review.py --region us      # 美股大盘
  python market_review.py --no-notify      # 不发送推送
  python market_review.py --force-run      # 跳过交易日检查
  python market_review.py --debug          # 调试模式
""")

    parser.add_argument('--debug',
                        action='store_true',
                        help='启用调试模式，输出详细日志')

    parser.add_argument('--region',
                        choices=['cn', 'us', 'both'],
                        default=None,
                        help='指定市场地区 (cn/us/both，默认从 config 读取)')

    parser.add_argument('--no-notify',
                        action='store_true',
                        help='不发送推送通知')

    parser.add_argument('--force-run',
                        action='store_true',
                        help='跳过交易日检查，强制执行')

    return parser.parse_args()


def run_market_review_standalone(
    config: Config = None,
    region: str = None,
    no_notify: bool = False,
    force_run: bool = False,
    notifier: NotificationService = None,
    analyzer: GeminiAnalyzer = None,
    search_service: SearchService = None,
) -> str:
    """
    运行独立大盘复盘（可作为函数导入使用）

    Args:
        config: 配置对象（可选）
        region: 覆盖地区 cn/us/both（可选）
        no_notify: 不发送通知（可选）
        force_run: 跳过交易日检查（可选）
        notifier: 复用外部通知服务（可选）
        analyzer: 复用外部分析器（可选）
        search_service: 复用外部搜索服务（可选）

    Returns:
        复盘报告文本
    """
    config = config or get_config()

    # 交易日过滤（仅在独立模式下，force_run 跳过）
    effective_region = None
    if not force_run and config.trading_day_check_enabled:
        open_markets = get_open_markets_today()
        target_region = (
            region
            if region is not None
            else (getattr(config, 'market_review_region', 'cn') or 'cn')
        )
        effective_region = compute_effective_region(target_region, open_markets)
        if effective_region == '':
            logging.getLogger(__name__).info(
                '今日大盘复盘相关市场均为非交易日，跳过执行。'
            )
            return ''

    # 初始化组件（如果未传入外部组件）
    if notifier is None:
        notifier = NotificationService()

    if search_service is None and (
        config.bocha_api_keys
        or config.tavily_api_keys
        or config.brave_api_keys
        or config.serpapi_keys
        or config.minimax_api_keys
        or config.searxng_base_urls
    ):
        search_service = SearchService(
            bocha_keys=config.bocha_api_keys,
            tavily_keys=config.tavily_api_keys,
            brave_keys=config.brave_api_keys,
            serpapi_keys=config.serpapi_keys,
            minimax_keys=config.minimax_api_keys,
            searxng_base_urls=config.searxng_base_urls,
            news_max_age_days=config.news_max_age_days,
            news_strategy_profile=getattr(config, 'news_strategy_profile', 'short'),
        )

    if analyzer is None and config.llm_model_list:
        analyzer = GeminiAnalyzer()
        if not analyzer.is_available():
            logging.getLogger(__name__).warning(
                'AI分析器初始化后不可用，请检查 LLM 配置'
            )
            analyzer = None

    # 调用大盘复盘核心
    report = run_market_review(
        notifier=notifier,
        analyzer=analyzer,
        search_service=search_service,
        send_notification=not no_notify,
        override_region=effective_region if effective_region else region,
    )
    return report or ''


def main():
    args = parse_args()
    config = get_config()
    setup_logging(log_prefix='market_review', debug=args.debug, log_dir=config.log_dir)
    logger = logging.getLogger(__name__)
    logger.info('========== 大盘复盘独立模式启动 ==========')

    try:
        report = run_market_review_standalone(
            config=config,
            region=args.region,
            no_notify=args.no_notify,
            force_run=args.force_run,
        )
        if report:
            logger.info('========== 大盘复盘完成 ==========')
        return 0
    except Exception as e:
        logger.exception(f'大盘复盘执行异常: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
