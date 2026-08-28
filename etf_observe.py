# -*- coding: utf-8 -*-
"""
==================================
ETF 周度观察报告 — 估值导向
==================================

定位：每周一次，默认纯观察；`--execute` 时执行统一调仓批次。

报告结构：
  1. 市场估值概览（全市场 PE + 国债收益率）
  2. 买入优先级（按 PE 分位排序，越便宜越靠前）
  3. 持仓对照与调仓建议（核心口径）
  4. 卫星仓 — ETF 火箭（放量突破候选 + 调仓建议）
  5. 动量观察（卫星仓背景排名，仅观察不交易）
  6. 自动调仓执行结果（仅 --execute：卫星卖 → 核心卖 → 核心买 → 卫星买）

卖出警示（极端条件触发：全市场 PE>90% + 行业 PE>95% + 拥挤）。
"""

import argparse
import io
import logging
import os
import re
import sys
from datetime import datetime
from typing import List

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import is_trading_day

setup_env()

from src.etf.config import CORE_BASELINE, AssetType
from src.etf.amazing_factors import (
    get_market_pe, get_treasury_yield_y10,
    rank_buy_priorities, check_sell_warnings,
)
from src.mx.client import is_mx_untradable

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

    # 收集所有核心仓权益 ETF（去重）
    equity_etfs = {}
    for a in CORE_BASELINE:
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
    for a in CORE_BASELINE:
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
    # 入口统一构造数据 manager，传给需要行情数据的下游模块（避免各自重复构造）
    from data_provider import get_fetcher
    fm = get_fetcher()
    rebalancer = ETFRebalancer(client, fetcher=fm)
    target = rebalancer.calculate_target(equity_offset=offset)
    core_positions, rotation_mv, rotation_positions = rebalancer.split_rotation_positions(positions)
    core_assets = max(total_assets - rotation_mv, 0.0)
    orders, total_deviation = rebalancer.compare(
        target, positions, total_assets, gate_state="", hard_intercept=False,
    )
    _should, reason = rebalancer.should_rebalance(orders, total_deviation, gate_state="")

    # 卫星仓 — ETF 火箭（只出指令，执行统一走 _execute_batch）
    rocket = None
    regime, hard_intercept = "chaos", False
    try:
        from src.analysis.market_gate import check_market_gate, fetch_gate_inputs

        _can, _cond, _sum, regime, hard_intercept = check_market_gate(fetch_gate_inputs(fm))
    except Exception:
        logger.warning("市场门控获取失败，卫星仓禁买", exc_info=True)
    try:
        from src.etf import rocket_breakout as rocket_mod

        rocket = rocket_mod.analyze_satellite(positions, total_assets,
                                              hard_intercept, regime, client=client)
    except Exception:
        logger.warning("卫星仓分析失败", exc_info=True)

    return {
        "client": client,
        "balance": balance,
        "positions": positions,
        "offset": offset,
        "pe_pct": pe_pct,
        "current_pe": current_pe,
        "total_assets": total_assets,
        "core_assets": core_assets,
        "core_positions": core_positions,
        "rotation_mv": rotation_mv,
        "rotation_positions": rotation_positions,
        "rebalancer": rebalancer,
        "target": target,
        "orders": orders,
        "total_deviation": total_deviation,
        "reason": reason,
        "rocket": rocket,
        "regime": regime,
        "hard_intercept": hard_intercept,
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
    core_assets = alloc["core_assets"]
    rotation_mv = alloc["rotation_mv"]
    rotation_positions = alloc["rotation_positions"]
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

    assets_line = f"**总资产**: {total_assets:,.0f} 元"
    if rotation_mv > 0:
        assets_line += f" | **核心口径**: {core_assets:,.0f} 元 | **卫星持仓**: {rotation_mv:,.0f} 元"
    lines.append(f"{assets_line} | {gate_line}")
    lines.append(f"**再平衡结论**: {reason}")
    lines.append("")

    # 当前持仓占比（核心口径：卫星持仓独立预算，不参与核心偏离计算）
    current_map: dict = {}
    for p in alloc["core_positions"]:
        mv = float(p.get("market_value", 0) or 0)
        if core_assets > 0:
            current_map[p.get("code", "")] = mv / core_assets * 100

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

    # 卫星持仓与基准外持仓提示
    if rotation_positions:
        lines.append("")
        lines.append("> 卫星持仓（独立预算，未纳入核心对照）："
                     + "、".join(f"{p.get('name', '')}({p.get('code', '')})" for p in rotation_positions))
    baseline_codes = {a.code for a in rebalancer.baseline}
    extra = [f"{p.get('name', '')}({p.get('code', '')})" for p in alloc["core_positions"]
             if p.get("code") not in baseline_codes]
    if extra:
        lines.append("")
        lines.append(f"> 基准外持仓：{'、'.join(extra)}（未纳入对照）")

    if orders:
        lines.append("")
        lines.append("**建议调仓指令（仅供参考，不自动执行）**")
        lines.append("")
        for o in orders:
            manual_mark = " ⚠️ 妙想无法交易，需手动" if is_mx_untradable(o.code) else ""
            lines.append(f"- {o.action.upper()} {o.name}({o.code}) {o.quantity}股 ≈ {o.amount:,.0f}元 — {o.reason}{manual_mark}")
    else:
        lines.append("")
        lines.append("无调仓需求，保持当前配置。")

    lines.append("")
    return "\n".join(lines)


# ── 卫星仓与动量观察 ──

def _satellite_overview(rocket: dict) -> str:
    """卫星仓 — ETF 火箭：信号候选 + 持仓 + 建议"""
    if not rocket:
        return ""

    lines = ["## 四、卫星仓 — ETF 火箭", ""]
    lines.append(f"预算 10% | 最多 2 只 | 当前卫星持仓市值 {rocket['satellite_mv']:,.0f} 元")
    if rocket["locked"]:
        lines.append("🔒 市场门控硬拦截，卫星仓锁定（禁买、清仓）")
    lines.append("")

    lines.append("### 火箭信号候选（放量突破）")
    lines.append("")
    cands = sorted([r for r in rocket["results"] if r["breakout"]],
                   key=lambda x: x["rocket_score"], reverse=True)
    if not cands:
        lines.append("无放量突破信号")
    for r in cands:
        pe = f"{r['pe_pct']:.0f}%" if r.get("pe_pct") is not None else "—"
        lines.append(
            f"- {r['name']}({r['code']}) 评分{r['rocket_score']} 涨幅{r['chg_pct']:+.1f}% "
            f"量比5日{r['vol_ratio_5']:.1f}/20日{r['vol_ratio_20']:.1f} 行业PE分位{pe}"
        )
    lines.append("")

    lines.append("### 持仓与建议")
    lines.append("")
    manual_codes = {
        o.code for o in (list(rocket.get("sells", [])) + list(rocket.get("buy_orders", [])))
        if is_mx_untradable(o.code)
    }

    def _note_line(n: str, prefix: str) -> str:
        m = re.search(r"\((\d{6})\)", n)
        mark = " ⚠️ 需手动" if m and m.group(1) in manual_codes else ""
        return f"- {prefix}：{n}{mark}"

    if rocket["sell_notes"]:
        for n in rocket["sell_notes"]:
            lines.append(_note_line(n, "🔴 卖出"))
    if rocket["buy_notes"]:
        for n in rocket["buy_notes"]:
            lines.append(_note_line(n, "🟢 买入"))
    if not rocket["sell_notes"] and not rocket["buy_notes"]:
        lines.append("无调仓建议")
    lines.append("")
    return "\n".join(lines)


def _momentum_observe(rocket: dict) -> str:
    """动量观察：卫星仓背景排名（只观察，不交易）"""
    if not rocket:
        return ""
    ranked = rocket["ranked"][:6]
    if not ranked:
        return ""
    lines = ["## 五、动量观察（卫星仓背景，仅排名不交易）", ""]
    for i, r in enumerate(ranked, 1):
        pe = f"PE分位{r['pe_pct']:.0f}%" if r.get("pe_pct") is not None else "PE—"
        lines.append(
            f"{i}. {r['name']}({r['code']}) 动量分{r['rot_score']} "
            f"20日{r['ret_20d']:+.1f}% 5日{r['ret_5d']:+.1f}% {pe}"
        )
    lines.append("")
    return "\n".join(lines)


# ── 自动调仓执行 ──

def _execute_batch(alloc: dict) -> str:
    """执行调仓批次（卫星卖 → 核心卖 → 核心买 → 卫星买，带全批次资金校验）"""
    from src.mx.client import MXMoniClient, is_mx_untradable, MX_UNTRADABLE_REASON
    from src.etf import rocket_breakout as rocket_mod

    core_orders = alloc["orders"]
    rocket = alloc.get("rocket")
    total_assets = alloc["total_assets"]
    avail_balance = alloc["balance"].get("avail_balance") or 0

    sat_sells = list(rocket["sells"]) if rocket else []
    sat_buys = list(rocket["buy_orders"]) if rocket else []
    core_sells = [o for o in core_orders if o.action == "sell"]
    core_buys = [o for o in core_orders if o.action == "buy"]
    sells = sat_sells + core_sells
    buys = core_buys + sat_buys

    lines = ["## 六、自动调仓执行结果", ""]

    if not sells and not buys:
        lines.append("无调仓指令，本次不执行。")
        return "\n".join(lines)

    def _qty(o):
        return getattr(o, "quantity", None) or getattr(o, "shares", 0)

    # 安全校验 1：卖出数量不得超过实际持仓
    held = {p.get("code", ""): int(p.get("count", 0) or 0) for p in alloc["positions"]}
    for o in sells:
        if _qty(o) > held.get(o.code, 0):
            lines.append(f"❌ 中止执行：{o.name}({o.code}) 卖出 {_qty(o)} 股 > 持仓 {held.get(o.code, 0)} 股")
            return "\n".join(lines)

    # 安全校验 2：买入总额不得超过可用资金 + 卖出回款
    sell_amount = sum(o.amount for o in sells)
    buy_amount = sum(o.amount for o in buys)
    if buy_amount > avail_balance + sell_amount:
        lines.append(f"❌ 中止执行：买入总额 {buy_amount:,.0f} 元 > 可用资金 {avail_balance:,.0f} 元 + 卖出回款 {sell_amount:,.0f} 元")
        return "\n".join(lines)

    # 安全校验 3：卫星买入不超 10% 预算
    if sat_buys:
        budget = total_assets * rocket_mod.SATELLITE_BUDGET_RATIO
        sat_buy_total = sum(o.amount for o in sat_buys)
        sat_sell_total = sum(o.amount for o in sat_sells)
        if sat_buy_total > budget - rocket["satellite_mv"] + sat_sell_total:
            lines.append(f"❌ 中止执行：卫星买入 {sat_buy_total:,.0f} 元 超预算上限 {budget:,.0f} 元")
            return "\n".join(lines)

    results: List[str] = []
    client = alloc["client"]

    # 1 开头深市 ETF/LOF 妙想无法交易：跳过 API，转为手动待办
    all_orders = sells + buys
    executable = [o for o in all_orders if not is_mx_untradable(o.code)]
    manual = [o for o in all_orders if is_mx_untradable(o.code)]

    # 先卖后买（卫星卖 → 核心卖 → 核心买 → 卫星买）
    for order in executable:
        qty = _qty(order)
        resp = client.trade(
            trade_type=order.action,
            stock_code=order.code,
            quantity=qty,
            use_market_price=True,
        )
        ok = resp is not None and resp.get("code") in ("0", "200")
        mark = "✅" if ok else "❌"
        msg = (resp or {}).get("message", "未知错误")
        results.append(f"{mark} {order.action.upper()} {order.name}({order.code}) {qty}股 ≈ {order.amount:,.0f}元 —— {'成功' if ok else f'失败: {msg}'}")

    lines.append(f"**总资产**: {total_assets:,.0f} 元 | **可用资金**: {avail_balance:,.0f} 元")
    lines.append(
        f"本次执行 {len(executable)} 笔（卖出 {sum(1 for o in executable if o.action == 'sell')}，"
        f"买入 {sum(1 for o in executable if o.action == 'buy')}）"
        + (f"，另有 {len(manual)} 笔需手动" if manual else "")
    )
    lines.append("")
    lines.extend(f"- {r}" for r in results)
    lines.append("")

    if manual:
        lines.append("### ⚠️ 需手动处理（妙想无法交易 1 开头深市 ETF/LOF）")
        lines.append("")
        lines.append(f"> {MX_UNTRADABLE_REASON}，请登录妙想 App 手动操作：")
        lines.append("")
        for o in manual:
            verb = "买入" if o.action == "buy" else "卖出"
            lines.append(f"- 手动{verb} {o.name}({o.code}) {_qty(o)}股 ≈ {o.amount:,.0f}元 — {o.reason}")
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
        _satellite_overview(alloc.get("rocket")),
        _momentum_observe(alloc.get("rocket")),
    ]

    rocket = alloc.get("rocket")
    has_orders = bool(alloc and alloc["orders"])
    has_sat_orders = bool(rocket and (rocket["sells"] or rocket["buy_orders"]))
    if execute and alloc and (has_orders or has_sat_orders):
        sections.append(_execute_batch(alloc))

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
