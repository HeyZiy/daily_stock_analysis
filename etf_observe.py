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

from src.config import setup_env
from src.logging_config import setup_logging

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


# ── 报告生成 ──

def _generate_report() -> str:
    """生成完整周报"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    sections = [
        f"# ETF 周度观察 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {now}",
        "",
        _market_overview(),
        _buy_priority(),
    ]

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
    args = parser.parse_args()

    setup_logging(log_prefix="etf_observe", debug=args.debug)

    logger.info("=" * 60)
    logger.info("ETF 周度观察报告")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        report = _generate_report()
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
