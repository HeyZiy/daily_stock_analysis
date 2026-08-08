# -*- coding: utf-8 -*-
"""
===================================
ETF 周度观察报告
===================================

定位：每周一次，两部分内容：
  1. PE 估值分析 → 长期核心仓的阶段性加减建议
  2. 板块轮动打分 → 卫星仓最近可关注的行业/主题 ETF

不执行交易、不对比持仓，纯观察建议。

使用方式：
    python etf_observe.py                    # 生成周报
    python etf_observe.py --no-notify        # 不发送通知
    python etf_observe.py --debug            # 调试模式
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import List

from src.config import setup_env
from src.logging_config import setup_logging

setup_env()

from src.etf.allocation_gate import check_allocation_gate
from src.etf.config import CORE_BASELINE, SATELLITE_POOL, AssetType
from src.etf.sector_rotation import score_all_pool

logger = logging.getLogger(__name__)


# ── PE 估值分析 ──

def _pe_section(equity_offset: float, pe_pct: float, current_pe: float,
                gate_summary: str) -> str:
    """生成 PE 估值分析章节"""
    # 估值水平
    level = ("极度低估" if pe_pct < 20 else
             "低估" if pe_pct < 40 else
             "合理" if pe_pct < 60 else
             "高估" if pe_pct < 80 else "极度高估")

    # 核心仓 ETF 名单（按类别）
    equities = [a for a in CORE_BASELINE if a.asset_type == AssetType.EQUITY]
    gold = [a for a in CORE_BASELINE if a.asset_type == AssetType.GOLD]
    cash = [a for a in CORE_BASELINE if a.asset_type == AssetType.CASH]

    lines = ["## 一、PE 估值与仓位建议", "", gate_summary, ""]

    # 加减仓建议
    if equity_offset > 0.05:
        action = "**超配权益，减少现金**"
        detail = [
            f"当前 PE {pe_pct:.0f}% 分位，{level}，建议权益仓位 +{equity_offset*100:.0f}%。",
            "",
            "可考虑加仓：",
        ]
        for e in equities:
            detail.append(f"  - {e.name}（{e.code}）")
        detail.append("")
        detail.append(f"资金从现金/逆回购挪出。")
        if gold:
            detail.append(f"黄金（{', '.join(a.name for a in gold)}）维持不变。")
    elif equity_offset < -0.05:
        action = "**减配权益，增加现金**"
        detail = [
            f"当前 PE {pe_pct:.0f}% 分位，{level}，建议权益仓位 {equity_offset*100:.0f}%。",
            "",
            "可考虑减仓（按波动从高到低优先）：",
        ]
        # 按波动率排名，高波动优先减
        sorted_eq = sorted(equities, key=lambda e: e.volatility_rank)
        for e in sorted_eq:
            detail.append(f"  - {e.name}（{e.code}）波动排名 {e.volatility_rank}")
        detail.append("")
        detail.append("减仓资金转入现金/逆回购。")
        if gold:
            detail.append(f"黄金（{', '.join(a.name for a in gold)}）维持不变。")
    else:
        action = "**维持中性**"
        detail = [
            f"当前 PE {pe_pct:.0f}% 分位，{level}，偏移仅 {equity_offset*100:+.0f}%，无需大幅调整。",
        ]

    lines.append(f"### {action}")
    lines.extend(detail)
    lines.append("")
    lines.append(f"> 核心仓共 {len(equities)} 只权益 ETF + {len(gold)} 只黄金 + 现金，中性权重下权益占 {sum(e.neutral_weight for e in equities)*100:.0f}%。")

    return "\n".join(lines)


# ── 板块轮动 ──

def _rotation_section(pe_pct: float) -> str:
    """生成板块轮动章节"""
    lines = ["## 二、板块轮动 — 本周关注", ""]

    if pe_pct >= 80:
        lines.append(f"PE 分位 {pe_pct:.0f}%，极度高估，建议暂停卫星仓操作。")
        return "\n".join(lines)

    if pe_pct >= 60:
        lines.append(f"PE 分位 {pe_pct:.0f}%，偏高估，卫星仓建议谨慎参与。")
        lines.append("")

    logger.info(f"对 {len(SATELLITE_POOL)} 只卫星 ETF 打分...")
    scores = score_all_pool(SATELLITE_POOL)
    valid = [s for s in scores if not s.get("error") and s["score"] > 0]

    if not valid:
        lines.append("暂无有效打分数据。")
        return "\n".join(lines)

    # 排名表
    lines.append("| 排名 | ETF | 代码 | 得分 | 20日涨跌 | 5日涨跌 | 均线 | 量能 |")
    lines.append("|------|-----|------|------|----------|---------|------|------|")
    for i, s in enumerate(valid, 1):
        ma_status = "多排" if s.get("ma_align", 0) >= 25 else \
                     "偏多" if s.get("ma_align", 0) >= 12 else \
                     "弱" if s.get("ma_align", 0) > 0 else "空"
        vol_status = "放量" if s.get("vol_ok", 0) >= 15 else \
                     "正常" if s.get("vol_ok", 0) >= 5 else "缩量"
        lines.append(
            f"| {i} | {s['name']} | {s['code']} | "
            f"{s['score']} | {s.get('ret_20d', 0):+.1f}% | {s.get('ret_5d', 0):+.1f}% | "
            f"{ma_status} | {vol_status} |"
        )

    # 推荐关注
    lines.append("")
    top_n = min(3, len(valid))
    lines.append(f"### 建议关注前 {top_n}")
    lines.append("")
    for i in range(top_n):
        s = valid[i]
        lines.append(
            f"- **{s['name']}**（{s['code']}）得分 {s['score']} | "
            f"20日 {s.get('ret_20d', 0):+.1f}% 5日 {s.get('ret_5d', 0):+.1f}%"
        )

    return "\n".join(lines)


# ── 报告生成 ──

def _generate_report() -> str:
    """生成完整周报"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1. PE 估值
    equity_offset, pe_pct, current_pe, gate_summary = check_allocation_gate()
    pe_body = _pe_section(equity_offset, pe_pct, current_pe, gate_summary)

    # 2. 板块轮动
    rotation_body = _rotation_section(pe_pct)

    report = "\n\n".join([
        f"# ETF 周度观察 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {now}",
        "",
        pe_body,
        rotation_body,
        "---",
        "",
        "*免责声明：本报告仅供观察参考，不构成投资建议。*",
    ])

    return report


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

    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    _save_report(report)

    if not args.no_notify:
        _send_notification(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
