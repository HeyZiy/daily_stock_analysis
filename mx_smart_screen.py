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

class MXStockManager:
    """妙想选股与自选股管理类"""

    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw"

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化

        Args:
            api_key: 妙想API密钥，如果不提供则从项目配置读取（.env文件中的MX_APIKEY）
        """
        if api_key:
            self.api_key = api_key
        else:
            # 从项目配置读取
            config = get_config()
            self.api_key = config.mx_apikey

        if not self.api_key:
            raise ValueError(
                "请设置MX_APIKEY。方式1: 在项目根目录.env文件中添加 MX_APIKEY=your_api_key\n"
                "方式2: 设置环境变量 MX_APIKEY=your_api_key\n"
                "方式3: 初始化时传入 api_key 参数"
            )

        self.headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key
        }

    def get_self_selected_stocks(self, include_raw: bool = False) -> List[Stock]:
        """
        获取自选股列表

        Args:
            include_raw: 是否包含原始数据中的所有字段

        Returns:
            自选股列表
        """
        url = f"{self.BASE_URL}/self-select/get"

        try:
            response = requests.post(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != 0:
                print(f"获取自选股失败: {data.get('message', '未知错误')}")
                return []

            result = data.get("data", {}).get("allResults", {}).get("result", {})
            data_list = result.get("dataList", [])

            stocks = []
            for item in data_list:
                stock = self._parse_stock_item(item, include_raw)
                stocks.append(stock)

            return stocks

        except requests.RequestException as e:
            print(f"请求异常: {e}")
            return []
        except json.JSONDecodeError as e:
            print(f"JSON解析异常: {e}")
            return []

    def get_self_selected_stocks_raw(self) -> Optional[Dict[str, Any]]:
        """
        获取自选股的原始响应数据（用于查看所有可用字段）

        Returns:
            原始API响应数据
        """
        url = f"{self.BASE_URL}/self-select/get"

        try:
            response = requests.post(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"获取原始数据失败: {e}")
            return None

    def select_stocks(self, keyword: str, page_no: int = 1, page_size: int = 20) -> List[Stock]:
        """
        执行妙想选股

        Args:
            keyword: 选股条件（自然语言描述）
            page_no: 页码，默认1
            page_size: 每页数量，默认20

        Returns:
            选股结果列表
        """
        url = f"{self.BASE_URL}/stock-screen"
        payload = {
            "keyword": keyword,
            "pageNo": page_no,
            "pageSize": page_size
        }

        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != 0:
                print(f"选股失败: {data.get('message', '未知错误')}")
                return []

            result_data = data.get("data", {}).get("data", {}).get("result", {})
            data_list = result_data.get("dataList", [])

            stocks = []
            for item in data_list:
                stock = self._parse_stock_item(item)
                stocks.append(stock)

            return stocks

        except requests.RequestException as e:
            print(f"请求异常: {e}")
            return []
        except json.JSONDecodeError as e:
            print(f"JSON解析异常: {e}")
            return []

    def add_to_self_select(self, query: str) -> bool:
        """
        添加股票到自选股

        Args:
            query: 操作指令（自然语言描述，如"把贵州茅台加入自选"）

        Returns:
            是否成功
        """
        url = f"{self.BASE_URL}/self-select/manage"
        payload = {"query": query}

        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == 0:
                print(f"添加成功: {query}")
                return True
            else:
                print(f"添加失败: {data.get('message', '未知错误')}")
                return False

        except requests.RequestException as e:
            print(f"请求异常: {e}")
            return False
        except json.JSONDecodeError as e:
            print(f"JSON解析异常: {e}")
            return False

    def add_stocks_to_self_select(self, stocks: List[Stock]) -> int:
        """
        批量添加股票到自选股

        Args:
            stocks: 股票列表

        Returns:
            成功添加的数量
        """
        success_count = 0
        for stock in stocks:
            query = f"把{stock.name}加入自选"
            if self.add_to_self_select(query):
                success_count += 1
        return success_count

    def select_and_add(self, keyword: str, page_no: int = 1, page_size: int = 20) -> int:
        """
        执行选股并添加到自选股

        Args:
            keyword: 选股条件
            page_no: 页码
            page_size: 每页数量

        Returns:
            成功添加的股票数量
        """
        print(f"正在执行选股: {keyword}")
        stocks = self.select_stocks(keyword, page_no, page_size)

        if not stocks:
            print("未找到符合条件的股票")
            return 0

        print(f"选股完成，找到 {len(stocks)} 只股票")
        print("股票列表:")
        for stock in stocks:
            print(f"  - {stock}")

        print(f"\n开始添加到自选股...")
        success_count = self.add_stocks_to_self_select(stocks)
        print(f"成功添加 {success_count}/{len(stocks)} 只股票到自选股")

        return success_count

    def _parse_stock_item(self, item: Dict[str, Any], include_raw: bool = False) -> Stock:
        """解析股票数据项"""
        # 提取已知字段
        stock = Stock(
            code=item.get("SECURITY_CODE", ""),
            name=item.get("SECURITY_SHORT_NAME", ""),
            market=item.get("MARKET_SHORT_NAME", ""),
            newest_price=self._parse_float(item.get("NEWEST_PRICE")),
            chg=self._parse_float(item.get("CHG")),
            pchg=self._parse_float(item.get("PCHG")),
        )

        # 尝试提取其他可能的字段（字段名可能包含前缀，如 010000_TURNOVER_RATE...）
        for key, value in item.items():
            if "TURNOVER_RATE" in key:
                stock.turnover_rate = self._parse_float(value)
            elif "LIANGBI" in key:
                stock.liangbi = self._parse_float(value)
            elif key.endswith("VOLUME") and "TRADING" not in key:
                stock.volume = self._parse_int(value)
            elif "TRADING_VOLUMES" in key:
                stock.trading_volume = self._parse_float(value)
            elif "PE_D" in key:
                stock.pe_d = self._parse_float(value)
            elif "PB" in key and "PCHG" not in key:
                stock.pb = self._parse_float(value)
            elif "TOAL_MARKET_VALUE" in key or "TOTAL_MARKET_VALUE" in key:
                stock.total_market_value = self._parse_float(value)
            elif "CIRCULATION_MARKET" in key:
                stock.circulation_market_value = self._parse_float(value)

        if include_raw:
            stock.extra_data = item

        return stock

    @staticmethod
    def _parse_float(value: Any) -> Optional[float]:
        """解析浮点数"""
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_int(value: Any) -> Optional[int]:
        """解析整数"""
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None


def list_self_selected_stocks(config: Config) -> None:
    """列出当前自选股"""

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
