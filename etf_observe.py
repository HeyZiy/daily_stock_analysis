# -*- coding: utf-8 -*-
"""
===================================
ETF 周度观察报告 — 估值导向
===================================

定位：每周一次，纯市场观察
  1. 市场估值概览（全市场 PE + 国债收益率）
  2. 买入优先级（按 PE 分位排序，越便宜越靠前）
  3. 卖出警示（极端条件触发：全市场 PE>90% + 行业 PE>95% + 拥挤）

不执行交易、不对比持仓、不生成买卖指令。
"""

import argparse
import io
import logging
import os
import sys
from datetime import datetime
from typing import List

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import is_trading_day

setup_env()

from src.etf.config import CORE_BASELINE, SATELLITE_POOL, AssetType
from src.etf.amazing_factors import (
    get_market_pe, get_treasury_yield_y10,
    rank_buy_priorities, check_sell_warnings,
)

logger = logging.getLogger(__name__)


# ── 市场概览 ──

def _market_overview() -> str:
    """市场估值概览（仅列数据，不给操作建议）"""
    lines = ["## 一、市场估值概览", ""]

    market_pe = get_market_pe()
    if market_pe:
        pe, pe_pct = market_pe["pe"], market_pe["pe_pct"]
        level = ("极度低估" if pe_pct < 20 else
                 "低估" if pe_pct < 40 else
                 "合理" if pe_pct < 60 else
                 "偏贵" if pe_pct < 80 else "高估")
        lines.append(f"- **全市场 PE**：{pe:.1f} 倍 | 近 5 年 **{pe_pct:.0f}%** 分位 →  **{level}**")
        lines.append(f"- **股票内在收益率（1/PE）**：{100/pe:.2f}%")

        y10 = get_treasury_yield_y10()
        if y10:
            spread = 100/pe - y10
            lines.append(
                f"- **风险溢价**：{100/pe:.2f}% − 国债 {y10:.2f}% = **{spread:+.2f}%** "
                f"（{'承担风险有额外回报' if spread > 0 else '股票还不如国债'}）"
            )

        if pe_pct < 30:
            lines.append(f"- **定投节奏**：低估区间，可适当加量")
        elif pe_pct < 70:
            lines.append(f"- **定投节奏**：正常")
        else:
            lines.append(f"- **定投节奏**：偏贵区间，建议放缓、多留现金")
    else:
        lines.append("- **全市场 PE**：数据不可用")
        y10 = get_treasury_yield_y10()
        if y10:
            lines.append(f"- **10 年期国债收益率**：{y10:.2f}%")

    lines.append("")
    return "\n".join(lines)


# ── 买入优先级 ──

def _buy_priority() -> str:
    """买入优先级（按 PE 分位排序，越便宜越靠前）"""
    lines = ["## 二、买入优先级（按估值便宜度排序）", ""]

    # 收集所有权益 ETF（核心 + 卫星，去重）
    equity_etfs = {}
    for a in CORE_BASELINE + SATELLITE_POOL:
        if a.asset_type == AssetType.EQUITY and a.code not in equity_etfs:
            equity_etfs[a.code] = a

    ranked = rank_buy_priorities(list(equity_etfs.values()))

    lines.append("| ETF | 估值基准 | PE 分位 | 建议 |")
    lines.append("|-----|---------|--------|------|")

    for r in ranked:
        source = r.get("source_name", "") or "—"
        pe_display = f"{r['pe_pct']:.0f}%" if r.get("pe_pct") is not None else "—"
        lines.append(
            f"| {r['name']}（{r['code']}） | {source} | {pe_display} | "
            f"{r['level']} {r['level_text']} |"
        )

    lines.append("")
    return "\n".join(lines)


# ── 卖出警示 ──

def _sell_warning() -> str:
    """卖出警示（极端条件才触发）"""
    equity_etfs = {}
    for a in CORE_BASELINE + SATELLITE_POOL:
        if a.asset_type == AssetType.EQUITY and a.code not in equity_etfs:
            equity_etfs[a.code] = a

    warnings = check_sell_warnings(list(equity_etfs.values()))
    if not warnings:
        return ""

    lines = ["## ⚠️ 卖出警示", ""]
    lines.append("> 以下 ETF 触发长期配置回避条件：")
    lines.append("")
    for w in warnings:
        lines.append(f"- **{w['name']}**（{w['code']}）：{w['reason']}")
    lines.append("")
    return "\n".join(lines)


# ── 持仓对照与调仓建议 ──

def _compute_allocation():
    """计算目标配比 + 调仓指令（供对照展示与自动执行复用）

    Returns:
        dict 或 None（MX 不可用/无数据时）
    """
    if not os.getenv("MX_APIKEY"):
        return None

    from src.etf.allocation_gate import check_allocation_gate
    from src.etf.rebalancer import ETFRebalancer
    from src.mx.client import MXMoniClient

    client = MXMoniClient()
    balance = client.get_balance()
    if not balance or (balance.get("total_assets") or 0) <= 0:
        return None

    positions = client.get_positions()

    # 估值门控 → 权益偏移 → 目标配比
    try:
        offset, pe_pct, current_pe, _gate_summary = check_allocation_gate()
    except Exception:
        logger.warning("估值门控计算失败，按中性配置对照", exc_info=True)
        offset, pe_pct, current_pe = 0.0, None, None

    total_assets = balance["total_assets"]
    rebalancer = ETFRebalancer(client)
    target = rebalancer.calculate_target(equity_offset=offset)
    orders, total_deviation = rebalancer.compare(
        target, positions, total_assets, gate_state="", hard_intercept=False,
    )
    _should, reason = rebalancer.should_rebalance(orders, total_deviation, gate_state="")

    return {
        "client": client,
        "balance": balance,
        "positions": positions,
        "offset": offset,
        "pe_pct": pe_pct,
        "current_pe": current_pe,
        "total_assets": total_assets,
        "rebalancer": rebalancer,
        "target": target,
        "orders": orders,
        "total_deviation": total_deviation,
        "reason": reason,
    }


def _holding_overview(alloc=None) -> str:
    """持仓 vs 目标对照 + 调仓建议（只出建议，不自动执行）"""
    lines = ["## 三、持仓对照与调仓建议", ""]

    if alloc is None:
        alloc = _compute_allocation()
    if alloc is None:
        if not os.getenv("MX_APIKEY"):
            lines.append("- 未配置 MX_APIKEY，跳过持仓对照")
        else:
            lines.append("- 妙想账户暂无数据，跳过持仓对照")
        return "\n".join(lines)

    client = alloc["client"]
    balance = alloc["balance"]
    positions = alloc["positions"]
    offset = alloc["offset"]
    pe_pct = alloc["pe_pct"]
    total_assets = alloc["total_assets"]
    rebalancer = alloc["rebalancer"]
    target = alloc["target"]
    orders = alloc["orders"]
    reason = alloc["reason"]

    if pe_pct is not None:
        level = ("极度低估" if pe_pct < 20 else
                 "低估" if pe_pct < 40 else
                 "合理" if pe_pct < 60 else
                 "偏贵" if pe_pct < 80 else "高估")
        gate_line = f"**权益偏移**: {'+' if offset >= 0 else ''}{offset*100:.0f}% | PE 分位 {pe_pct:.0f}%（{level}）"
    else:
        gate_line = f"**权益偏移**: {'+' if offset >= 0 else ''}{offset*100:.0f}%（中性对照）"

    lines.append(f"**总资产**: {total_assets:,.0f} 元 | {gate_line}")
    lines.append(f"**再平衡结论**: {reason}")
    lines.append("")

    # 当前持仓占比（仅来自妙想实际持仓）
    current_map: dict = {}
    for p in positions:
        mv = float(p.get("market_value", 0) or 0)
        if total_assets > 0:
            current_map[p.get("code", "")] = mv / total_assets * 100

    # 现金实际占比 = 剩余资金（妙想持仓不含现金项）
    held_sum = sum(v for k, v in current_map.items() if k != "CASH")
    current_map["CASH"] = max(0.0, 100.0 - held_sum)

    lines.append("| 资产 | 目标% | 实际% | 偏离 | 建议 |")
    lines.append("|------|-------|-------|------|------|")

    order_map = {o.code: o for o in orders}
    for asset in rebalancer.baseline:
        code = asset.code
        tgt = target.get(code, 0.0) * 100
        cur = current_map.get(code, 0.0)
        dev = tgt - cur
        order = order_map.get(code)
        action = ""
        if order:
            action = f"{'🟢买' if order.action == 'buy' else '🔴卖'} {order.quantity}股"
        lines.append(f"| {asset.name} | {tgt:.1f}% | {cur:.1f}% | {dev:+.1f}% | {action} |")

    # 基准外持仓提示
    baseline_codes = {a.code for a in rebalancer.baseline}
    extra = [f"{p.get('name', '')}({p.get('code', '')})" for p in positions
             if p.get("code") not in baseline_codes]
    if extra:
        lines.append("")
        lines.append(f"> 基准外持仓：{'、'.join(extra)}（未纳入对照）")

    if orders:
        lines.append("")
        lines.append("**建议调仓指令（仅供参考，不自动执行）**")
        lines.append("")
        for o in orders:
            lines.append(f"- {o.action.upper()} {o.name}({o.code}) {o.quantity}股 ≈ {o.amount:,.0f}元 — {o.reason}")
    else:
        lines.append("")
        lines.append("无调仓需求，保持当前配置。")

    lines.append("")
    return "\n".join(lines)


# ── 自动调仓执行 ──

def _execute_rebalance(alloc: dict) -> str:
    """执行调仓指令（先卖后买，带资金安全校验），返回执行结果摘要"""
    from src.mx.client import MXMoniClient

    rebalancer = alloc["rebalancer"]
    orders = alloc["orders"]
    total_assets = alloc["total_assets"]
    avail_balance = alloc["balance"].get("avail_balance") or 0

    lines = ["## 四、自动调仓执行结果", ""]

    if not orders:
        lines.append("无调仓指令，本次不执行。")
        return "\n".join(lines)

    sells = [o for o in orders if o.action == "sell"]
    buys = [o for o in orders if o.action == "buy"]

    # 安全校验 1：买入总额不得超过可用资金（先卖后买，卖出到账计入）
    sell_amount = sum(o.amount for o in sells)
    buy_amount = sum(o.amount for o in buys)
    usable = avail_balance + sell_amount
    if buy_amount > usable:
        lines.append(f"❌ 中止执行：买入总额 {buy_amount:,.0f} 元 > 可用资金 {avail_balance:,.0f} 元 + 卖出回款 {sell_amount:,.0f} 元")
        return "\n".join(lines)

    # 安全校验 2：卖出数量不得超过实际持仓
    held = {p.get("code", ""): int(p.get("count", 0) or 0) for p in alloc["positions"]}
    for o in sells:
        if o.quantity > held.get(o.code, 0):
            lines.append(f"❌ 中止执行：{o.name}({o.code}) 卖出 {o.quantity} 股 > 持仓 {held.get(o.code, 0)} 股")
            return "\n".join(lines)

    results: List[str] = []

    # 先卖后买（避免现金不足）
    for order in sells + buys:
        resp = alloc["client"].trade(
            trade_type=order.action,
            stock_code=order.code,
            quantity=order.quantity,
            use_market_price=True,
        )
        ok = resp is not None and resp.get("code") in ("0", "200")
        mark = "✅" if ok else "❌"
        msg = (resp or {}).get("message", "未知错误")
        results.append(f"{mark} {order.action.upper()} {order.name}({order.code}) {order.quantity}股 ≈ {order.amount:,.0f}元 —— {'成功' if ok else f'失败: {msg}'}")

    lines.append(f"**总资产**: {total_assets:,.0f} 元 | **可用资金**: {avail_balance:,.0f} 元")
    lines.append(f"本次执行 {len(sells) + len(buys)} 笔（卖出 {len(sells)}，买入 {len(buys)}）：")
    lines.append("")
    lines.extend(f"- {r}" for r in results)
    lines.append("")

    # 执行后复核持仓
    fresh = MXMoniClient().get_positions()
    if fresh:
        lines.append("**执行后持仓**：")
        lines.append("")
        for p in fresh:
            mv = float(p.get("market_value", 0) or 0)
            pct = mv / total_assets * 100 if total_assets > 0 else 0
            lines.append(f"- {p.get('name', '')}({p.get('code', '')}) {p.get('count', 0)}股 ≈ {mv:,.0f}元（{pct:.1f}%）")
        lines.append("")

    return "\n".join(lines)


# ── 报告生成 ──

def _generate_report(execute: bool = False) -> str:
    """生成完整周报（execute=True 时自动执行调仓并附结果）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    alloc = _compute_allocation()
    holding_section = _holding_overview(alloc)

    sections = [
        f"# ETF 周度观察 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {now}",
        "",
        _market_overview(),
        _buy_priority(),
        holding_section,
    ]

    if execute and alloc and alloc["orders"]:
        sections.append(_execute_rebalance(alloc))

    warn = _sell_warning()
    if warn:
        sections.append(warn)

    sections.extend([
        "---",
        "",
        "*免责声明：本报告仅供观察参考，不构成投资建议。*",
    ])

    return "\n\n".join(sections)


def _save_report(report: str) -> str:
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    report_path = os.path.join(reports_dir, f"etf_weekly_{today_str}.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f"周报已保存: {report_path}")
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


def main():
    parser = argparse.ArgumentParser(description='ETF 周度观察报告')
    parser.add_argument('--no-notify', action='store_true', help='不发送通知')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    parser.add_argument('--execute', action='store_true', help='自动执行调仓指令（默认只出建议）')
    parser.add_argument('--force', action='store_true', help='跳过交易日检查（手动调试用）')
    args = parser.parse_args()

    setup_logging(log_prefix="etf_observe", debug=args.debug)

    # 交易日检查：非交易日且未 --force 时直接跳过（节假日周一）
    if not args.force and not is_trading_day():
        logger.info("今天不是 A 股交易日，跳过执行")
        print("今天不是 A 股交易日，跳过执行")
        return 0

    logger.info("=" * 60)
    logger.info("ETF 周度观察报告" + ("（自动调仓）" if args.execute else ""))
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        report = _generate_report(execute=args.execute)
    except Exception as e:
        logger.exception(f"生成报告失败: {e}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    _save_report(report)

    if not args.no_notify:
        _send_notification(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
