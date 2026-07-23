# -*- coding: utf-8 -*-
"""
===================================
趋势交易策略 — 日度分析与信号检测
===================================

定位：趋势波段系统。只做主线中的强趋势股，只在分歧回踩时介入。

职责：
1. 读取妙想自选股或指定股票列表，直接进行技术分析
2. 市场环境过滤（调用 market_gate 模块）
3. 纯技术分析（第一次分歧回踩MA5等规则）
4. 观察池维护（趋势破坏自动剔除）

核心策略：
- 买点：主升中的第一次分歧回踩MA5（缩量 + 不破5日线 + 换手率>5%）
- 不做：加速追高、情绪高潮接力、连续大阳后追涨
- 第一卖点(减仓50%)：放量跌破5日线 / 高位长阴 / 回撤≥5%
- 第二卖点(清仓)：跌破10日线 / 放量跌破10日线
- 环境过滤：见 docs/market.md
- 趋势不走坏即保留，连续2天跌破10日线才剔除

使用方式：
    python trend_analysis.py                    # 正常运行
    python trend_analysis.py --debug            # 调试模式
    python trend_analysis.py --no-notify        # 不发送通知
    python trend_analysis.py --stocks CODE1,CODE2  # 指定股票分析
    python trend_analysis.py --max-stocks N     # 最多分析N只股票（按跌幅排序）
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from data_provider.base import DataFetcherManager, canonical_stock_code
from src.analysis.market_gate import check_market_gate
from src.config import setup_env
from src.notify.service import NotificationService
from src.mx.service import MXService
from src.analysis.analyzer import StockTrendAnalyzer
from src.analysis.strategy.removal_rules import check_removal_rules
from src.analysis.strategy.signal_detector import TechnicalSignal, detect_pullback_signals
from src.analysis.report import generate_technical_report
setup_env()

logger = logging.getLogger(__name__)


class SimpleTechnicalAnalyzer:
    """
    简化版技术分析器
    
    不依赖 LLM，纯技术指标计算
    """
    
    def __init__(self):
        self.fetcher = DataFetcherManager()
        self.mx_service = MXService()
        self.trend_analyzer = StockTrendAnalyzer()  # 复用 main.py 的技术指标计算
        self._trading_dates_cache = None
    
    def get_trading_dates(self, start_date: date, end_date: date) -> List[date]:
        if self._trading_dates_cache is not None:
            return [d for d in self._trading_dates_cache if start_date <= d <= end_date]
        
        try:
            import akshare as ak
            cal_df = ak.tool_trade_date_hist_sina()
            trading_dates = []
            for _, row in cal_df.iterrows():
                trade_date = pd.to_datetime(row['trade_date']).date()
                if start_date <= trade_date <= end_date:
                    trading_dates.append(trade_date)
            self._trading_dates_cache = trading_dates
            return trading_dates
        except Exception as e:
            logger.error(f"严重错误：获取交易日历失败！无法进行后续精确计算。错误信息: {e}")
            raise RuntimeError("交易日历获取失败。")

    def get_stocks_pct_change(self, stock_list: List[Tuple[str, str]]) -> Dict[str, float]:
        """
        获取股票列表的涨跌幅

        Args:
            stock_list: [(code, name), ...]

        Returns:
            {code: pct_change} 涨跌幅百分比
        """
        pct_changes = {}
        for code, name in stock_list:
            try:
                quote = self.fetcher.get_realtime_quote(code)
                if quote and hasattr(quote, 'pct_chg'):
                    pct_changes[code] = float(quote.pct_chg) if quote.pct_chg is not None else 0.0
                else:
                    pct_changes[code] = 0.0
            except Exception as e:
                logger.debug(f"获取 {code} 涨跌幅失败: {e}")
                pct_changes[code] = 0.0
        return pct_changes

    def fetch_stock_data(self, code: str, days: int = 40) -> Optional[pd.DataFrame]:
        """
        获取股票历史数据（直接从网络获取）

        Args:
            code: 股票代码
            days: 获取天数

        Returns:
            DataFrame 或 None
        """
        try:
            end_date = date.today()
            start_date = end_date - timedelta(days=days)
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')

            result = self.fetcher.get_daily_data(code, start_str, end_str)
            if isinstance(result, tuple) and len(result) >= 1:
                df = result[0]
            else:
                df = result

            if df is not None and hasattr(df, 'empty') and not df.empty:
                df_latest_date = pd.to_datetime(df['date'].max()).date()
                trading_dates = self.get_trading_dates(end_date - timedelta(days=30), end_date)
                if trading_dates:
                    last_trading_day = trading_dates[-1]
                    if df_latest_date < last_trading_day:
                        logger.error(f"❌ {code} 网络获取的数据仍过期(最新:{df_latest_date}, 需要:{last_trading_day})")
                        return None
                return df
            else:
                logger.error(f"❌ {code} 从网络获取数据失败")
                return None

        except Exception as e:
            logger.warning(f"获取 {code} 数据失败: {e}")
            return None
    
    def analyze_all_stocks(self, stock_list: List[Tuple[str, str]], 
                          max_stocks: Optional[int] = None,
                          sort_by_pct: bool = True) -> Tuple[List[TechnicalSignal], List[Tuple[str, str, str]], List[Tuple[str, str, str]]]:
        """
        分析所有关注股票，返回技术信号列表、剔除列表和失败列表

        Args:
            stock_list: [(code, name), ...]
            max_stocks: 最大分析数量，超过则按跌幅排序取前N只
            sort_by_pct: 是否按跌幅排序（优先分析跌幅大的股票）

        Returns:
            (技术信号列表, [(code, name, 剔除原因), ...], [(code, name, 失败原因), ...])
        """
        all_signals = []
        removed_stocks = []
        failed_stocks = []

        if max_stocks and len(stock_list) > max_stocks:
            if sort_by_pct:
                logger.info(f"获取涨跌幅数据，股票数量 {len(stock_list)} 超过限制 {max_stocks}，按跌幅排序...")
                pct_changes = self.get_stocks_pct_change(stock_list)
                sorted_stocks = sorted(stock_list, key=lambda x: pct_changes.get(x[0], 0))
                stock_list = sorted_stocks[:max_stocks]
                logger.info(f"已选取跌幅最大的 {max_stocks} 只股票进行分析")
            else:
                stock_list = stock_list[:max_stocks]

        logger.info(f"开始处理 {len(stock_list)} 只股票...")

        for i, (code, name) in enumerate(stock_list):
            try:
                df = self.fetch_stock_data(code)

                # 统一计算 MA，避免在剔除检查和信号检测中重复计算
                if df is not None and len(df) >= 10:
                    df = df.sort_values('date').reset_index(drop=True)
                    df = self.trend_analyzer._calculate_mas(df)

                should_remove, remove_reason = check_removal_rules(code, df)
                if should_remove:
                    removed_stocks.append((code, name, remove_reason))
                    logger.info(f"❌ 剔除 {name}({code}): {remove_reason}")
                    continue

                signals = detect_pullback_signals(code, name, df)
                all_signals.extend(signals)

                if signals:
                    logger.info(f"✅ {name}({code}): 发现 {len(signals)} 个信号")

                if (i + 1) % 10 == 0:
                    logger.info(f"进度: {i + 1}/{len(stock_list)}")

            except Exception as e:
                logger.warning(f"分析 {name}({code}) 失败: {e}")
                failed_stocks.append((code, name, str(e)))
                continue

        all_signals.sort(key=lambda x: x.score, reverse=True)

        kept_count = len(stock_list) - len(removed_stocks) - len(failed_stocks)
        logger.info(f"处理完成 | 保留:{kept_count} 剔除:{len(removed_stocks)} 失败:{len(failed_stocks)} 信号:{len(all_signals)}")
        return all_signals, removed_stocks, failed_stocks


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='简化版趋势跟踪系统 - 无 LLM（集成模拟交易）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试模式'
    )

    parser.add_argument(
        '--no-notify',
        action='store_true',
        help='不发送推送通知'
    )

    parser.add_argument(
        '--stocks',
        type=str,
        help='指定要分析的股票代码，逗号分隔（覆盖妙想自选股）'
    )

    parser.add_argument(
        '--max-stocks',
        type=int,
        default=None,
        help='每天最多分析多少只股票（按跌幅排序优先分析跌幅大的）'
    )

    trade_group = parser.add_argument_group('交易模式（可选）')
    trade_group.add_argument(
        '--trade',
        action='store_true',
        help='盘后分析模式：分析技术信号并生成次日交易计划'
    )
    trade_group.add_argument(
        '--trade-execute',
        action='store_true',
        help='盘中执行模式：检查持仓止损止盈，执行买入'
    )
    trade_group.add_argument(
        '--trade-plan',
        action='store_true',
        help='查看当前交易计划'
    )

    return parser.parse_args()


def _save_report(report: str) -> str:
    """将报告保存到文件并返回路径。"""
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    report_path = os.path.join(reports_dir, f"technical_simple_{today_str}.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f"报告已保存: {report_path}")
    return report_path


def _send_notification(report: str) -> bool:
    """发送通知，如果已配置且可用。"""
    notifier = NotificationService()
    if not notifier.is_available():
        logger.warning("通知服务未配置")
        return False
    success = notifier.send(report)
    if success:
        logger.info("通知发送成功")
    else:
        logger.warning("通知发送失败")
    return success


def main():
    """主入口"""
    args = parse_arguments()
    
    # 配置日志
    from src.logging_config import setup_logging
    setup_logging(log_prefix="stock_analysis_simple", debug=args.debug)
    
    logger.info("=" * 60)
    logger.info("趋势交易策略 — 日度分析启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    try:
        analyzer = SimpleTechnicalAnalyzer()
        
        max_stocks = args.max_stocks
        if max_stocks is None:
            max_stocks = int(os.getenv('MAX_STOCKS_PER_DAY', '0')) or None
        
        # 1. 获取股票列表（从妙想或命令行）
        if args.stocks:
            # 使用命令行指定的股票
            stock_codes = [canonical_stock_code(c) for c in args.stocks.split(',') if c.strip()]
            name_mapping = {code: code for code in stock_codes}
            logger.info(f"使用指定股票列表: {stock_codes}")
        else:
            # 从妙想获取
            stock_codes, name_mapping = analyzer.mx_service.fetch_self_selected()
        
        if not stock_codes:
            logger.error("没有获取到股票列表，退出")
            return 1
        
        # 2. 技术分析（包含剔除检查）
        stock_list = list(zip(stock_codes, [name_mapping.get(c, c) for c in stock_codes]))
        logger.info(f"当前关注列表: {len(stock_list)} 只股票")

        signals, removed_stocks, failed_stocks = analyzer.analyze_all_stocks(stock_list, max_stocks=max_stocks, sort_by_pct=False)

        # 3. 从妙想删除剔除的股票（仅当不是命令行指定模式时）
        if removed_stocks and not args.stocks:
            removed_codes = [code for code, _, _ in removed_stocks]
            success = analyzer.mx_service.remove_stocks(removed_codes)
            if success:
                logger.info(f"已从妙想删除 {len(removed_codes)} 只自选股")
            else:
                logger.warning("从妙想删除失败")
        
        # 4. 市场环境检查 + 调节信号评分
        can_trade, market_conditions, market_summary, market_regime, hard_intercept = check_market_gate()
        logger.info(market_summary)
        if hard_intercept:
            logger.warning("硬拦截触发！当日应清仓所有持仓，不执行任何买入操作")

        # 根据市场环境调节信号有效评分
        regime_modifiers = {
            "trending_up": 1.0,
            "weak_up": 0.85,
            "sideways": 0.8,
            "trending_down": 0.5,
            "chaos": 0.0,
        }
        regime_modifier = regime_modifiers.get(market_regime, 0.85)
        regime_labels = {
            "trending_up": "趋势上行×1.0",
            "weak_up": "弱上行×0.85",
            "sideways": "震荡横盘×0.8",
            "trending_down": "趋势下行×0.5",
            "chaos": "混沌×不开仓",
        }
        regime_note = regime_labels.get(market_regime, "状态不明×0.85")
        for s in signals:
            s.effective_score = int(s.score * regime_modifier)
            s.regime_note = regime_note

        report = generate_technical_report(signals, removed_stocks,
                                           market_env=(can_trade, market_conditions, market_summary, market_regime),
                                           failed_stocks=failed_stocks)

        # 5. 保存报告
        _save_report(report)

        # 6. 发送通知
        if not args.no_notify:
            _send_notification(report)
        
        logger.info("运行完成")
        return 0
        
    except Exception as e:
        logger.exception(f"运行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
