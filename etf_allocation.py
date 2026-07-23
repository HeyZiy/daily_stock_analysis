# -*- coding: utf-8 -*-
"""
===================================
ETF 长期配置 — 日度再平衡分析
===================================

定位：主账户压舱石。根据市场 gate 状态计算战术偏移，对比 mx-moni 模拟仓持仓，
生成再平衡建议。

两种模式：
  - 分析模式（默认）：盘后跑，用收盘价算目标，出报告 + 保存调仓计划
  - 执行模式（--execute）：盘中午后跑，读上次分析的计划，按市价执行

使用方式：
    python etf_allocation.py                    # 分析模式（盘后 ~16:00）
    python etf_allocation.py --execute          # 执行模式（盘中 9:30-15:00）
    python etf_allocation.py --no-notify        # 不发送通知
    python etf_allocation.py --debug            # 调试模式

数据时效：
  - 分析模式：全部用 akshare 日线收盘价（盘后可用，无需行情实时性）
  - 执行模式：mx-moni useMarketPrice=true 市价成交（必须在盘中）
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional

from src.config import setup_env
from src.logging_config import setup_logging

setup_env()

from src.analysis.market_gate import check_market_gate
from src.mx.client import MXMoniClient
from src.etf.rebalancer import ETFRebalancer
from src.etf.config import NEUTRAL_BASELINE

logger = logging.getLogger(__name__)

TARGET_FILE = Path(__file__).parent / "data" / "etf_target.json"

# A 股交易时段（北京时间）
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(15, 0)


def _is_market_open() -> bool:
    """简单判断当前是否在 A 股交易时段"""
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _save_target(target: Dict[str, float], gate_state: str, hard_intercept: bool):
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "gate_state": gate_state,
        "hard_intercept": hard_intercept,
        "target": {k: round(v, 6) for k, v in target.items()},
    }
    TARGET_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"调仓计划已保存: {TARGET_FILE}")


def _load_target() -> Optional[dict]:
    if not TARGET_FILE.exists():
        return None
    return json.loads(TARGET_FILE.read_text(encoding="utf-8"))


def _save_report(report: str) -> str:
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    report_path = os.path.join(reports_dir, f"etf_allocation_{today_str}.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f"报告已保存: {report_path}")
    return report_path


def _send_notification(report: str) -> bool:
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


def _check_mx_setup() -> Optional[MXMoniClient]:
    if not os.environ.get("MX_APIKEY"):
        logger.error("未配置 MX_APIKEY，无法访问模拟仓")
        return None
    mx = MXMoniClient()
    balance = mx.get_balance()
    if not balance:
        logger.error("无法获取模拟仓数据，请检查 MX_APIKEY 和网络")
        return None
    logger.info(f"模拟仓总资产: {balance['total_assets']:,.0f} 元 | 可用: {balance['avail_balance']:,.0f} 元")
    return mx


def run_analysis(args):
    """分析模式：盘后运行，出报告 + 保存计划"""
    # 1. Gate
    can_trade, gate_conditions, gate_summary, gate_state, hard_intercept = check_market_gate()
    logger.info(f"Gate: {gate_state}" + (" + 硬拦截" if hard_intercept else ""))
    logger.info(gate_summary)

    # 2. 持仓
    mx = _check_mx_setup()
    if not mx:
        return 1

    balance = mx.get_balance()
    total_assets = balance["total_assets"]
    positions = mx.get_positions()

    # 3. 计算 + 比较
    rebalancer = ETFRebalancer(mx)
    target = rebalancer.calculate_target(gate_state, hard_intercept)
    orders, total_deviation = rebalancer.compare(target, positions, total_assets, gate_state, hard_intercept)

    # 4. 保存计划（供 --execute 读取）
    _save_target(target, gate_state, hard_intercept)

    # 5. 出报告
    report = rebalancer.generate_report(
        target, positions, total_assets, gate_state, hard_intercept,
        orders, total_deviation
    )
    should_exec, reason = rebalancer.should_rebalance(orders, total_deviation, gate_state)
    report += f"\n\n> **再平衡判断**: {'需要' if should_exec else '不需要'} — {reason}"
    report += "\n> 下次执行: 下个交易日盘中 `python etf_allocation.py --execute`\n"

    print("\n" + report)
    _save_report(report)
    if not args.no_notify:
        _send_notification(report)

    return 0


def run_execute():
    """执行模式：盘中运行，读上次计划，市价执行"""
    if not _is_market_open():
        logger.error("当前不在 A 股交易时段（9:30-15:00），无法执行调仓")
        return 1

    target_data = _load_target()
    if not target_data:
        logger.error(f"未找到调仓计划文件 {TARGET_FILE}，请先运行分析模式")
        return 1

    gate_state = target_data["gate_state"]
    hard_intercept = target_data.get("hard_intercept", False)
    target = target_data["target"]

    # gate 可能已变化，重新检查
    _, _, _, current_gate, current_hard = check_market_gate()
    gate_changed = (current_gate != gate_state) or (current_hard != hard_intercept)
    if gate_changed:
        logger.warning(
            f"Gate 已变化（{gate_state} → {current_gate}"
            + ("+硬拦截" if current_hard else "")
            + "），建议重新运行分析模式后再执行"
        )

    mx = _check_mx_setup()
    if not mx:
        return 1

    balance = mx.get_balance()
    total_assets = balance["total_assets"]
    positions = mx.get_positions()

    rebalancer = ETFRebalancer(mx)
    orders, total_deviation = rebalancer.compare(target, positions, total_assets, current_gate, current_hard)

    should_exec, reason = rebalancer.should_rebalance(orders, total_deviation, current_gate)
    logger.info(f"再平衡判断: {'需要' if should_exec else '不需要'} — {reason}")

    if not should_exec:
        logger.info("偏离在阈值内，无需执行")
        return 0

    # 从 mx-moni 持仓获取实时价格后再算一次数量（盘后用的收盘价，盘需更新）
    for order in orders:
        for p in positions:
            if p["code"] == order.code:
                price = p.get("current_price", 0) or 0
                if price > 0:
                    order.amount = price * order.quantity
                    break

    logger.info(f"准备执行 {len(orders)} 笔调仓...")
    results = rebalancer.execute_orders(orders)
    success = sum(1 for r in results if r["success"])
    fail = len(results) - success

    logger.info(f"调仓完成: 成功 {success}, 失败 {fail}")
    if fail > 0:
        for r in results:
            if not r["success"]:
                logger.warning(f"失败: {r['order'].code} {r['order'].action}: {r['response']}")

    return 0 if fail == 0 else 1


def main():
    parser = argparse.ArgumentParser(description='ETF 长期配置 — 日度再平衡')
    parser.add_argument('--execute', action='store_true', help='执行模式（盘中运行，按市价调仓）')
    parser.add_argument('--no-notify', action='store_true', help='不发送通知')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    args = parser.parse_args()

    setup_logging(log_prefix="etf_allocation", debug=args.debug)

    mode = "执行调仓" if args.execute else "分析"
    logger.info("=" * 60)
    logger.info(f"ETF 长期配置 — {mode}模式")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.execute:
        logger.info(f"市场状态: {'交易中' if _is_market_open() else '已收盘'}")
    logger.info("=" * 60)

    try:
        if args.execute:
            return run_execute()
        else:
            return run_analysis(args)
    except Exception as e:
        logger.exception(f"执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
