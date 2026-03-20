#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===================================
妙想智能选股模块
===================================

职责：
1. 基于自然语言条件执行智能选股
2. 与自选股列表合并管理
3. 提供命令行入口独立运行选股功能

使用方式：
    python mx_smart_screen.py                    # 使用配置文件的选股条件
    python mx_smart_screen.py "今日涨幅2%的股票"  # 指定选股条件
    python mx_smart_screen.py --list             # 列出当前自选股
    python mx_smart_screen.py --add-to-self      # 选股并添加到自选股
"""

import os
import sys
import argparse
import logging
from typing import List, Optional

# 设置环境变量
from src.config import setup_env
setup_env()

from src.config import get_config, Config
from src.logging_config import setup_logging
from src.services.stock_screen_service import StockScreenService

logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='妙想智能选股工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python mx_smart_screen.py                    # 使用配置文件的选股条件
  python mx_smart_screen.py "今日涨幅2%的股票"  # 指定选股条件
  python mx_smart_screen.py --list             # 列出当前自选股
  python mx_smart_screen.py --add-to-self      # 选股并添加到自选股
        '''
    )

    parser.add_argument(
        'keyword',
        nargs='?',
        help='选股条件（自然语言描述，如"今日涨幅2%的股票"）'
    )

    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='仅列出当前自选股，不执行选股'
    )

    parser.add_argument(
        '--add-to-self', '-a',
        action='store_true',
        help='将选股结果添加到自选股'
    )

    parser.add_argument(
        '--page-size', '-n',
        type=int,
        default=20,
        help='选股结果数量限制（默认20）'
    )

    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        help='启用调试模式'
    )

    return parser.parse_args()


def get_screened_stocks(config: Config, keyword: str, page_size: int = 20) -> List[str]:
    """
    执行智能选股

    Args:
        config: 配置对象
        keyword: 选股条件
        page_size: 结果数量限制

    Returns:
        选中的股票代码列表
    """
    if not config.mx_apikey:
        logger.error("未配置 MX_APIKEY，智能选股功能不可用")
        logger.error("请在 .env 文件中添加: MX_APIKEY=your_api_key")
        return []

    screen_service = StockScreenService(config.mx_apikey)
    screened_codes = screen_service.get_screened_codes(keyword, max_count=page_size)

    return screened_codes


def list_self_selected_stocks(config: Config) -> None:
    """列出当前自选股"""
    from test_mx_stock_manager import MXStockManager

    try:
        manager = MXStockManager()
        stocks = manager.get_self_selected_stocks()

        if not stocks:
            logger.info("当前自选股为空")
            return

        logger.info(f"当前自选股共 {len(stocks)} 只:")
        for i, stock in enumerate(stocks, 1):
            logger.info(f"{i}. {stock.name}({stock.code}) {stock.market}")

    except Exception as e:
        logger.error(f"获取自选股失败: {e}")


def add_stocks_to_self_select(stock_codes: List[str]) -> int:
    """
    将股票添加到自选股

    Args:
        stock_codes: 股票代码列表

    Returns:
        成功添加的数量
    """
    from test_mx_stock_manager import MXStockManager

    try:
        manager = MXStockManager()
        success_count = 0

        for code in stock_codes:
            # 通过代码查询股票名称（这里简化处理，直接添加）
            query = f"把{code}加入自选"
            if manager.add_to_self_select(query):
                success_count += 1

        return success_count

    except Exception as e:
        logger.error(f"添加到自选股失败: {e}")
        return 0


def main() -> int:
    """
    主入口函数

    Returns:
        退出码（0 表示成功）
    """
    args = parse_arguments()

    # 加载配置
    config = get_config()

    # 配置日志
    setup_logging(log_prefix="smart_screen", debug=args.debug, log_dir=config.log_dir)

    logger.info("=" * 60)
    logger.info("妙想智能选股工具")
    logger.info("=" * 60)

    # 模式1: 仅列出自选股
    if args.list:
        logger.info("模式: 列出当前自选股")
        list_self_selected_stocks(config)
        return 0

    # 确定选股条件
    keyword = args.keyword
    if not keyword:
        keyword = config.smart_screen_keyword

    if not keyword:
        logger.error("未指定选股条件，请通过命令行参数或 SMART_SCREEN_KEYWORD 环境变量指定")
        logger.error("示例: python mx_smart_screen.py \"今日涨幅2%的股票\"")
        return 1

    logger.info(f"选股条件: {keyword}")

    # 执行选股
    screened_codes = get_screened_stocks(config, keyword, args.page_size)

    if not screened_codes:
        logger.warning("未找到符合条件的股票")
        return 0

    logger.info(f"选股完成，找到 {len(screened_codes)} 只股票: {screened_codes}")

    # 模式2: 添加到自选股
    if args.add_to_self:
        logger.info("正在添加到自选股...")
        success_count = add_stocks_to_self_select(screened_codes)
        logger.info(f"成功添加 {success_count}/{len(screened_codes)} 只股票到自选股")

    return 0


if __name__ == "__main__":
    sys.exit(main())
