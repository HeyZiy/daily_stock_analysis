# -*- coding: utf-8 -*-
"""
===================================
ETF 长期配置 — 日度再平衡分析
===================================

定位：主账户压舱石。根据市场 gate 状态计算战术偏移，对比持仓，生成再平衡建议。

两种持仓源：
  - MX 模拟仓模式（默认）：通过妙想 API 读取模拟仓持仓
  - 手动模式（--manual）：读取 data/positions.json，用户手动维护份额

使用方式：
    python etf_allocation.py                    # MX 模式（需要 MX_APIKEY）
    python etf_allocation.py --manual           # 手动模式（读取 positions.json）
    python etf_allocation.py --execute          # 盘中执行调仓（仅 MX 模式）
    python etf_allocation.py --no-notify        # 不发送通知
    python etf_allocation.py --debug            # 调试模式

手动模式文件格式（data/positions.json）：
    {
      "updated": "2026-07-24",
      "total_assets": 100000,
      "positions": {
        "510300": {"shares": 5000, "cost": 4.20},
        "511880": {"shares": 5000, "cost": 100.00}
      }
    }
    每次实际交易后手动更新 shares 即可。
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
from src.etf.allocation_gate import check_allocation_gate
from src.etf.sector_rotation import run_rotation
from src.etf.config import SATELLITE_POOL
from src.mx.client import MXMoniClient
from src.etf.rebalancer import ETFRebalancer
from src.etf.config import NEUTRAL_BASELINE

logger = logging.getLogger(__name__)

TARGET_FILE = Path(__file__).parent / "data" / "etf_target.json"
POSITIONS_FILE = Path(__file__).parent / "data" / "positions.json"

# A 股交易时段（北京时间）
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(15, 0)

# 手动模式下 ETF 价格缓存
_price_cache: Dict[str, float] = {}


def _fetch_etf_price(code: str) -> float:
    """获取 ETF 当前价格，优先收盘价，回退实时价"""
    if code in _price_cache:
        return _price_cache[code]
    # 1. akshare 日线收盘价
    try:
        import akshare as ak
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
        if df is not None and not df.empty:
            price = float(df.sort_values('date').iloc[-1]['close'])
            _price_cache[code] = price
            return price
    except Exception:
        pass
    # 2. DataFetcherManager 实时行情
    try:
        from data_provider.base import DataFetcherManager
        fm = DataFetcherManager()
        quote = fm.get_realtime_quote(code)
        if quote and hasattr(quote, 'price') and quote.price:
            price = float(quote.price)
            _price_cache[code] = price
            return price
    except Exception:
        pass
    return 0.0


def _load_manual_positions() -> Optional[dict]:
    """读取手动持仓文件，计算当前持仓。返回 (positions_list, total_assets) 或 None"""
    if not POSITIONS_FILE.exists():
        logger.error(f"持仓文件不存在: {POSITIONS_FILE}")
        logger.error("请先创建 data/positions.json，参考格式见脚本头部注释")
        return None

    raw = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    total_assets = float(raw.get("total_assets", 0) or 0)
    raw_positions = raw.get("positions", {})

    if total_assets <= 0:
        logger.error("positions.json 中的 total_assets 必须 > 0")
        return None

    positions = []
    for code, info in raw_positions.items():
        shares = int(info.get("shares", 0) or 0)
        cost = float(info.get("cost", 0) or 0)
        if shares <= 0:
            continue
        price = _fetch_etf_price(code)
        if price <= 0:
            logger.warning(f"无法获取 {code} 的行情价格，跳过")
            continue
        mv = shares * price
        positions.append({
            "code": code,
            "name": info.get("name", ""),
            "count": shares,
            "cost_price": cost,
            "current_price": price,
            "market_value": mv,
        })

    if not positions:
        logger.error("没有有效持仓数据")
        return None

    return {"positions": positions, "total_assets": total_assets}


def _auto_detect_mode(args) -> str:
    """自动检测运行模式：--manual 优先，否则检查 MX_APIKEY 决定"""
    if args.manual:
        return "manual"
    if not os.environ.get("MX_APIKEY"):
        logger.info("未设置 MX_APIKEY，自动切换为手动模式")
        return "manual"
    return "mx"


def _is_market_open() -> bool:
    """简单判断当前是否在 A 股交易时段"""
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _save_target(target: Dict[str, float], gate_state: str, hard_intercept: bool,
                  equity_offset: float = None):
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "gate_state": gate_state,
        "hard_intercept": hard_intercept,
        "target": {k: round(v, 6) for k, v in target.items()},
    }
    if equity_offset is not None:
        data["equity_offset"] = equity_offset
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


def run_analysis(args, mode: str):
    """分析模式：盘后运行，出报告 + 保存计划"""
    # 1. Gate：双门控
    #   PE 估值门控 → 决定 equity_offset（战略层：便宜多买，贵多卖）
    equity_offset, pe_pct, current_pe, gate_summary = check_allocation_gate()
    logger.info(gate_summary)
    #   趋势门控 → 提供执行上下文：阈值松紧、黄金/国债保护、硬拦截检测（战术层）
    _, _, _, gate_state, hard_intercept = check_market_gate()

    # 2. 持仓
    if mode == "manual":
        manual_data = _load_manual_positions()
        if not manual_data:
            logger.error("无法加载手动持仓，请在 data/positions.json 中填写实际持仓")
            return 1
        total_assets = manual_data["total_assets"]
        positions = manual_data["positions"]
        logger.info(f"手动模式 | 总资产: {total_assets:,.0f} 元 | 持仓 {len(positions)} 只")
    else:
        mx = _check_mx_setup()
        if not mx:
            return 1
        balance = mx.get_balance()
        total_assets = balance["total_assets"]
        positions = mx.get_positions()
        logger.info(f"MX 模拟仓 | 总资产: {total_assets:,.0f} 元 | 可用: {balance['avail_balance']:,.0f} 元")

    # 3. 计算 + 比较
    rebalancer = ETFRebalancer()
    target = rebalancer.calculate_target(equity_offset=equity_offset)
    orders, total_deviation = rebalancer.compare(target, positions, total_assets, gate_state, hard_intercept)

    # 4. 保存计划
    _save_target(target, gate_state, hard_intercept, equity_offset)

    # 4.5 板块轮动（卫星仓，独立于核心仓）
    rotation_section = ""
    if mode == "manual" and SATELLITE_POOL:
        try:
            rot_orders, rotation_section = run_rotation(
                SATELLITE_POOL, positions, total_assets, pe_pct
            )
            logger.info(f"板块轮动: {len(rot_orders)} 笔调仓")
        except Exception as e:
            logger.warning(f"板块轮动异常: {e}")

    # 5. 出报告
    report = rebalancer.generate_report(
        target, positions, total_assets, orders, total_deviation,
        equity_offset=equity_offset, pe_percentile=pe_pct, current_pe=current_pe,
    )
    should_exec, reason = rebalancer.should_rebalance(orders, total_deviation, gate_state)
    report += "\n\n---\n\n"
    report += gate_summary
    if rotation_section:
        report += "\n\n---\n\n"
        report += rotation_section
    report += f"\n\n> **再平衡判断**: {'需要' if should_exec else '不需要'} — {reason}"
    if mode == "mx":
        report += "\n> 下次执行: 下个交易日盘中 `python etf_allocation.py --execute`\n"
    else:
        report += "\n> 手动模式：请在券商执行调仓后更新 data/positions.json\n"

    print("\n" + report)
    _save_report(report)
    if not args.no_notify:
        _send_notification(report)

    return 0


def run_execute():
    """执行模式：盘中运行，读上次计划，市价执行（仅 MX 模式）"""
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
    parser.add_argument('--manual', action='store_true', help='手动模式（读取 data/positions.json）')
    parser.add_argument('--execute', action='store_true', help='执行模式（盘中运行，按市价调仓，仅 MX 模式）')
    parser.add_argument('--no-notify', action='store_true', help='不发送通知')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    args = parser.parse_args()

    setup_logging(log_prefix="etf_allocation", debug=args.debug)

    mode = _auto_detect_mode(args)

    if args.execute:
        if mode == "manual":
            logger.error("--execute 仅支持 MX 模式，手动模式请自行在券商操作后更新 positions.json")
            return 1
        action = "执行调仓"
    else:
        action = f"分析（{'手动' if mode == 'manual' else 'MX模拟仓'}）"

    logger.info("=" * 60)
    logger.info(f"ETF 长期配置 — {action}")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.execute:
        logger.info(f"市场状态: {'交易中' if _is_market_open() else '已收盘'}")
    logger.info("=" * 60)

    try:
        if args.execute:
            return run_execute()
        else:
            return run_analysis(args, mode)
    except Exception as e:
        logger.exception(f"执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
