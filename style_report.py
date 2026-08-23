# -*- coding: utf-8 -*-
"""
==================================
风格状态周报 — Style Report
==================================

定位：每周六上午跑一次（用周五收盘数据），纯规则判定市场风格状态，
不接交易引擎、不依赖 LLM。

报告结构（见 src/analysis/style_state.py）：
  1. 风格状态（主线强势期 / 退潮期 / 真空期 / 形成中 + 主导风格）
  2. 主线明细（领涨组 Top5 / 垫底组 / 持续性指标）
  3. 风格指标（大小盘 / 风格簇收益 / 市场宽度）
  4. 市场环境（复用市场门控）

用法：
    python style_report.py                       # 周报 + 通知
    python style_report.py --no-notify           # 只看报告
    python style_report.py --backtest 2021-01-01 # 历史回放状态时间线（不落盘不通知）
"""

import argparse
import io
import logging
import os
import sys
from datetime import datetime

from src.config import setup_env

setup_env()

from src.logging_config import setup_logging

logger = logging.getLogger(__name__)


def _save_report(report: str, prefix: str) -> str:
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    report_path = os.path.join(reports_dir, f"{prefix}_{today_str}.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f"报告已保存: {report_path}")
    return report_path


def _send_notification(report: str) -> bool:
    try:
        from src.notify.service import NotificationService
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
    except Exception as e:
        logger.warning(f"通知发送失败: {e}")
        return False


def main():
    # Windows 控制台 GBK 不认 emoji，强制 UTF-8 输出
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

    parser = argparse.ArgumentParser(description='风格状态周报（纯规则，无 LLM）')
    parser.add_argument('--no-notify', action='store_true', help='不发送通知')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    parser.add_argument('--backtest', type=str, nargs='?', const='2020-01-01', default=None,
                        metavar='YYYY-MM-DD', help='历史回放状态时间线（默认从 2020-01-01）')
    args = parser.parse_args()

    setup_logging(log_prefix="style_report", debug=args.debug)

    # 周六跑用周五收盘数据，不做交易日门控
    from src.analysis.style_state import run_weekly, run_backtest

    if args.backtest:
        logger.info(f"风格状态回测: 起点 {args.backtest}")
        try:
            report = run_backtest(args.backtest)
        except Exception as e:
            logger.exception(f"回测失败: {e}")
            return 1
        print("\n" + report)
        _save_report(report, "style_backtest")
        return 0

    logger.info("=" * 60)
    logger.info("风格状态周报")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        report, state = run_weekly()
    except Exception as e:
        logger.exception(f"生成周报失败: {e}")
        return 1

    print("\n" + report)
    _save_report(report, "style_weekly")

    if not args.no_notify:
        _send_notification(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
