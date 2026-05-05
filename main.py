# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 主调度程序
===================================

职责：
1. 协调各模块完成股票分析流程
2. 实现低并发的线程池调度
3. 全局异常处理，确保单股失败不影响整体
4. 提供命令行入口

使用方式：
    python main.py              # 正常运行
    python main.py --debug      # 调试模式

交易理念（已融入分析）：
- 严进策略：不追高，乖离率 > 5% 不买入
- 趋势交易：只做 MA5>MA10>MA20 多头排列
- 效率优先：关注筹码集中度好的股票
- 买点偏好：缩量回踩 MA5/MA10 支撑
"""
import os

from src.config import setup_env

setup_env()

# 代理配置 - 通过 USE_PROXY 环境变量控制，默认关闭
# GitHub Actions 环境自动跳过代理配置
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    # 本地开发环境，启用代理（可在 .env 中配置 PROXY_HOST 和 PROXY_PORT）
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

import argparse
import logging
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple, Dict

from data_provider.base import canonical_stock_code
from src.core.pipeline import StockAnalysisPipeline

from src.config import get_config, Config
from src.logging_config import setup_logging


logger = logging.getLogger(__name__)

import requests


def get_mx_self_selected_stocks(config: Config) -> Tuple[List[str], Dict[str, str]]:
    """
    从妙想获取自选股列表

    Args:
        config: 配置对象

    Returns:
        (股票代码列表, 股票代码到名称的映射字典)
    """
    if not config.mx_apikey:
        logger.warning("未配置 MX_APIKEY，无法获取妙想自选股")
        return [], {}


    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw"
    url = f"{BASE_URL}/self-select/get"
    headers = {
        "Content-Type": "application/json",
        "apikey": config.mx_apikey
    }

    try:
        response = requests.post(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != 0:
            logger.warning(f"获取妙想自选股失败: {data.get('message', '未知错误')}")
            return [], {}

        result = data.get("data", {}).get("allResults", {}).get("result", {})
        data_list = result.get("dataList", [])

        stock_codes = []
        name_mapping = {}
        for item in data_list:
            code = item.get("SECURITY_CODE")
            name = item.get("SECURITY_SHORT_NAME")
            if code:
                stock_codes.append(code)
                if name:
                    name_mapping[code] = name

        return stock_codes, name_mapping

    except requests.RequestException as e:
        logger.warning(f"请求妙想自选股异常: {e}")
        return [], {}
    except Exception as e:
        logger.warning(f"获取妙想自选股失败: {e}")
        return [], {}


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='A股自选股智能分析系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python main.py                    # 正常运行
  python main.py --debug            # 调试模式
  python main.py --stocks 600519,000001  # 指定分析特定股票
  python main.py --no-notify        # 不发送推送通知
  python main.py --single-notify    # 启用单股推送模式（每分析完一只立即推送）
  python main.py --schedule         # 启用定时任务模式
        '''
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试模式，输出详细日志'
    )

    parser.add_argument(
        '--stocks',
        type=str,
        help='指定要分析的股票代码，逗号分隔（覆盖配置文件）'
    )

    parser.add_argument(
        '--no-notify',
        action='store_true',
        help='不发送推送通知'
    )

    parser.add_argument(
        '--single-notify',
        action='store_true',
        help='启用单股推送模式：每分析完一只股票立即推送，而不是汇总推送'
    )

    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help='并发线程数（默认使用配置值）'
    )

    parser.add_argument(
        '--schedule',
        action='store_true',
        help='启用定时任务模式，每日定时执行'
    )

    parser.add_argument(
        '--no-run-immediately',
        action='store_true',
        help='定时任务启动时不立即执行一次'
    )

    parser.add_argument(
        '--force-run',
        action='store_true',
        help='跳过交易日检查，强制执行全量分析（Issue #373）'
    )

    parser.add_argument(
        '--webui',
        action='store_true',
        help='启动 Web 管理界面'
    )

    parser.add_argument(
        '--webui-only',
        action='store_true',
        help='仅启动 Web 服务，不执行自动分析'
    )

    parser.add_argument(
        '--serve',
        action='store_true',
        help='启动 FastAPI 后端服务（同时执行分析任务）'
    )

    parser.add_argument(
        '--serve-only',
        action='store_true',
        help='仅启动 FastAPI 后端服务，不自动执行分析'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=8000,
        help='FastAPI 服务端口（默认 8000）'
    )

    parser.add_argument(
        '--host',
        type=str,
        default='0.0.0.0',
        help='FastAPI 服务监听地址（默认 0.0.0.0）'
    )

    parser.add_argument(
        '--no-context-snapshot',
        action='store_true',
        help='不保存分析上下文快照'
    )

    # === Backtest ===
    parser.add_argument(
        '--backtest',
        action='store_true',
        help='运行回测（对历史分析结果进行评估）'
    )

    parser.add_argument(
        '--backtest-code',
        type=str,
        default=None,
        help='仅回测指定股票代码'
    )

    parser.add_argument(
        '--backtest-days',
        type=int,
        default=None,
        help='回测评估窗口（交易日数，默认使用配置）'
    )

    parser.add_argument(
        '--backtest-force',
        action='store_true',
        help='强制回测（即使已有回测结果也重新计算）'
    )

    return parser.parse_args()


def _compute_trading_day_filter(
    config: Config,
    args: argparse.Namespace,
    stock_codes: List[str],
) -> Tuple[List[str], bool]:
    """
    Compute filtered stock list based on trading days (Issue #373).

    Returns:
        (filtered_codes, should_skip_all)
        - should_skip_all: skip entire run when no stocks left
    """
    force_run = getattr(args, 'force_run', False)
    if force_run or not getattr(config, 'trading_day_check_enabled', True):
        return (stock_codes, False)

    from src.core.trading_calendar import (
        get_market_for_stock,
        get_open_markets_today,
    )

    open_markets = get_open_markets_today()
    filtered_codes = []
    for code in stock_codes:
        mkt = get_market_for_stock(code)
        if mkt in open_markets or mkt is None:
            filtered_codes.append(code)

    should_skip_all = not filtered_codes
    return (filtered_codes, should_skip_all)


def run_full_analysis(
    config: Config,
    args: argparse.Namespace,
    stock_codes: Optional[List[str]] = None,
    stock_name_mapping: Optional[Dict[str, str]] = None
):
    """
    执行个股分析流程

    这是定时任务调用的主函数
    """
    try:
        # Issue #529: Hot-reload STOCK_LIST from .env on each scheduled run
        if stock_codes is None:
            config.refresh_stock_list()

        # Issue #373: Trading day filter (per-stock, per-market)
        effective_codes = stock_codes if stock_codes is not None else config.stock_list
        filtered_codes, should_skip = _compute_trading_day_filter(
            config, args, effective_codes
        )
        if should_skip:
            logger.info(
                "今日所有相关市场均为非交易日，跳过执行。可使用 --force-run 强制执行。"
            )
            return
        if set(filtered_codes) != set(effective_codes):
            skipped = set(effective_codes) - set(filtered_codes)
            logger.info("今日休市股票已跳过: %s", skipped)
        stock_codes = filtered_codes

        # 命令行参数 --single-notify 覆盖配置（#55）
        if getattr(args, 'single_notify', False):
            config.single_stock_notify = True

        # 检查 LLM 配置，如果没有则报错退出
        has_llm_config = bool(
            getattr(config, 'gemini_api_keys', []) or
            getattr(config, 'deepseek_api_keys', []) or
            os.getenv('GEMINI_API_KEY') or
            os.getenv('DEEPSEEK_API_KEY')
        )
        if not has_llm_config:
            logger.error("未配置 LLM，无法进行股票分析。请在 .env 文件中配置 GEMINI_API_KEY 或 DEEPSEEK_API_KEY")
            sys.exit(1)

        # 创建调度器
        save_context_snapshot = None
        if getattr(args, 'no_context_snapshot', False):
            save_context_snapshot = False
        query_id = uuid.uuid4().hex
        pipeline = StockAnalysisPipeline(
            config=config,
            max_workers=args.workers,
            query_id=query_id,
            query_source="cli",
            save_context_snapshot=save_context_snapshot
        )

        # 将妙想自选股名称预填充到缓存（如果有）
        if stock_name_mapping:
            if not hasattr(pipeline.fetcher_manager, '_stock_name_cache'):
                pipeline.fetcher_manager._stock_name_cache = {}
            for code, name in stock_name_mapping.items():
                pipeline.fetcher_manager._stock_name_cache[code] = name
            logger.info(f"已预填充 {len(stock_name_mapping)} 只股票名称从妙想自选股")

        # 运行个股分析
        results = pipeline.run(
            stock_codes=stock_codes,
            send_notification=not args.no_notify,
            merge_notification=False
        )

        # 输出摘要
        if results:
            logger.info("\n===== 分析结果摘要 =====")
            for r in sorted(results, key=lambda x: x.sentiment_score, reverse=True):
                emoji = r.get_emoji()
                logger.info(
                    f"{emoji} {r.name}({r.code}): {r.operation_advice} | "
                    f"评分 {r.sentiment_score} | {r.trend_prediction}"
                )

        logger.info("\n任务执行完成")

        # === 新增：生成飞书云文档 ===
        try:
            from src.feishu_doc import FeishuDocManager

            feishu_doc = FeishuDocManager()
            if feishu_doc.is_configured() and results:
                logger.info("正在创建飞书云文档...")

                # 1. 准备标题
                tz_cn = timezone(timedelta(hours=8))
                now = datetime.now(tz_cn)
                doc_title = f"{now.strftime('%Y-%m-%d %H:%M')} 个股分析"

                # 2. 准备内容
                dashboard_content = pipeline.notifier.generate_aggregate_report(
                    results,
                    getattr(config, 'report_type', 'simple'),
                )
                full_content = f"# 🚀 个股决策仪表盘\n\n{dashboard_content}"

                # 3. 创建文档
                doc_url = feishu_doc.create_daily_doc(doc_title, full_content)
                if doc_url:
                    logger.info(f"飞书云文档创建成功: {doc_url}")
                    # 可选：将文档链接也推送到群里
                    if not args.no_notify:
                        pipeline.notifier.send(f"[{now.strftime('%Y-%m-%d %H:%M')}] 分析文档创建成功: {doc_url}")

        except Exception as e:
            logger.error(f"飞书文档生成失败: {e}")

        # === Auto backtest ===
        try:
            if getattr(config, 'backtest_enabled', False):
                from src.services.backtest_service import BacktestService

                logger.info("开始自动回测...")
                service = BacktestService()
                stats = service.run_backtest(
                    force=False,
                    eval_window_days=getattr(config, 'backtest_eval_window_days', 10),
                    min_age_days=getattr(config, 'backtest_min_age_days', 14),
                    limit=200,
                )
                logger.info(
                    f"自动回测完成: processed={stats.get('processed')} saved={stats.get('saved')} "
                    f"completed={stats.get('completed')} insufficient={stats.get('insufficient')} errors={stats.get('errors')}"
                )
        except Exception as e:
            logger.warning(f"自动回测失败（已忽略）: {e}")

    except Exception as e:
        logger.exception(f"分析流程执行失败: {e}")


def start_api_server(host: str, port: int, config: Config) -> None:
    """
    在后台线程启动 FastAPI 服务
    
    Args:
        host: 监听地址
        port: 监听端口
        config: 配置对象
    """
    import threading
    import uvicorn

    def run_server():
        level_name = (config.log_level or "INFO").lower()
        uvicorn.run(
            "api.app:app",
            host=host,
            port=port,
            log_level=level_name,
            log_config=None,
        )

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    logger.info(f"FastAPI 服务已启动: http://{host}:{port}")


def _is_truthy_env(var_name: str, default: str = "true") -> bool:
    """Parse common truthy / falsy environment values."""
    value = os.getenv(var_name, default).strip().lower()
    return value not in {"0", "false", "no", "off"}


def main() -> int:
    """
    主入口函数

    Returns:
        退出码（0 表示成功）
    """
    # 解析命令行参数
    args = parse_arguments()

    # 加载配置（在设置日志前加载，以获取日志目录）
    config = get_config()

    # 配置日志（输出到控制台和文件）
    setup_logging(log_prefix="stock_analysis", debug=args.debug, log_dir=config.log_dir)

    logger.info("=" * 60)
    logger.info("A股自选股智能分析系统 启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # 验证配置
    warnings = config.validate()
    for warning in warnings:
        logger.warning(warning)
    
    # 解析股票列表（统一为大写 Issue #355）
    # 1. 先读取妙想自选股作为基础股票池
    stock_codes, mx_name_mapping = get_mx_self_selected_stocks(config)
    if stock_codes:
        logger.info(f"从妙想自选股获取到 {len(stock_codes)} 只股票: {stock_codes}")
    else:
        logger.info("妙想自选股为空")

    # 2. 如果用户指定了额外股票，添加到股票池并去重
    if args.stocks:
        extra_codes = [canonical_stock_code(c) for c in args.stocks.split(',') if (c or "").strip()]
        # 合并并去重，保持原有顺序
        seen = set(stock_codes)
        for code in extra_codes:
            if code not in seen:
                stock_codes.append(code)
                seen.add(code)
        logger.info(f"用户指定股票 {len(extra_codes)} 只，合并后共 {len(stock_codes)} 只: {stock_codes}")

    # === 处理 --webui / --webui-only 参数，映射到 --serve / --serve-only ===
    if args.webui:
        args.serve = True
    if args.webui_only:
        args.serve_only = True

    # 兼容旧版 WEBUI_ENABLED 环境变量
    if config.webui_enabled and not (args.serve or args.serve_only):
        args.serve = True

    try:
        # 模式0: 回测
        if getattr(args, 'backtest', False):
            logger.info("模式: 回测")
            from src.services.backtest_service import BacktestService

            service = BacktestService()
            stats = service.run_backtest(
                code=getattr(args, 'backtest_code', None),
                force=getattr(args, 'backtest_force', False),
                eval_window_days=getattr(args, 'backtest_days', None),
            )
            logger.info(
                f"回测完成: processed={stats.get('processed')} saved={stats.get('saved')} "
                f"completed={stats.get('completed')} insufficient={stats.get('insufficient')} errors={stats.get('errors')}"
            )
            return 0

        # 模式1: 定时任务模式
        if args.schedule or config.schedule_enabled:
            logger.info("模式: 定时任务")
            logger.info(f"每日执行时间: {config.schedule_time}")

            # Determine whether to run immediately:
            # Command line arg --no-run-immediately overrides config if present.
            # Otherwise use config (defaults to True).
            should_run_immediately = config.schedule_run_immediately
            if getattr(args, 'no_run_immediately', False):
                should_run_immediately = False

            logger.info(f"启动时立即执行: {should_run_immediately}")

            from src.scheduler import run_with_schedule

            def scheduled_task():
                run_full_analysis(config, args, stock_codes, mx_name_mapping)

            run_with_schedule(
                task=scheduled_task,
                schedule_time=config.schedule_time,
                run_immediately=should_run_immediately
            )
            return 0

        # 模式3: 正常单次运行
        if config.run_immediately:
            run_full_analysis(config, args, stock_codes, mx_name_mapping)
        else:
            logger.info("配置为不立即运行分析 (RUN_IMMEDIATELY=false)")

        logger.info("\n程序执行完成")

        return 0

    except KeyboardInterrupt:
        logger.info("\n用户中断，程序退出")
        return 130

    except Exception as e:
        logger.exception(f"程序执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
