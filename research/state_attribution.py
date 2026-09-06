# -*- coding: utf-8 -*-
"""
==================================
状态 × 策略归因（v0，研究脚本）
==================================

回答 style_report 的初心验证问题：**状态标签有没有区分度？**
即：不同风格周期下，核心基准 / 动量 Top3 / 现金 的周收益分布是否不同。

方法：
- 状态标签：style_state.backtest_states() 逐周五重放（无未来函数）
- 核心列：CORE_BASELINE 权重 × 各 ETF 周收益（含现金权重，现金收益记 0）
- 动量列：每周五按动量代理分（20/60 日相对沪深300超额 + 行业成交额占比 5 日变化）
  选行业 Top3 等权持有一周，收益用**行业指数**收盘价（ETF 代理）
- 现金列：0

已知折扣（结论解读时必须记住）：
- 动量列是代理：不回放真实突破触发/止损/妙想约束，行业指数 ≠ ETF 净值
- 核心列用当前基准回放：有基准构成幸存者偏差
- 无交易成本；两融/份额流因子历史短，动量分不含
- v0 结论只回答"标签机制有没有区分度"，不回答实盘判定准确性

用法：
  python research/state_attribution.py --start 2021-01-01
"""

import argparse
import os
import sys
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import setup_env  # noqa: E402

setup_env()

from src.logging_config import setup_logging  # noqa: E402
from src.market_state.style_state import STATE_LABELS, backtest_states  # noqa: E402

logger = __import__("logging").getLogger(__name__)

HS300 = "000300"
TOP_N = 3
# 动量代理分权重（与 industry_momentum.momentum_score 同构：价能60 + 量能40）
W_EX20, W_EX60, W_FLOW, W_HEAT = 30, 30, 20, 20


def _weekly_returns(close: pd.Series, dates: List[str]) -> List[Optional[float]]:
    """给定收盘序列（日期索引）与周五序列，返回逐周收益（前视一周）。"""
    out = []
    for a, b in zip(dates, dates[1:]):
        try:
            pa, pb = close.get(a), close.get(b)
            out.append(float(pb) / float(pa) - 1 if pa and pb and pa > 0 else None)
        except (TypeError, ZeroDivisionError):
            out.append(None)
    return out


def _momentum_picks(as_of: str, ind_close: pd.DataFrame, ind_amount: pd.DataFrame,
                    hs300_close: pd.Series, ind_etf: Dict[str, str]) -> List[str]:
    """截至 as_of 的行业动量代理 Top N（返回行业指数代码列表）。"""
    scores = []
    sub_close = ind_close[ind_close.index <= as_of]
    sub_amt = ind_amt = ind_amount[ind_amount.index <= as_of]
    if len(sub_close) < 65:
        return []
    d = sub_close.index[-1]
    hs = hs300_close[hs300_close.index <= as_of]
    if len(hs) < 65:
        return []
    hs20 = float(hs.iloc[-1]) / float(hs.iloc[-21]) - 1
    hs60 = float(hs.iloc[-1]) / float(hs.iloc[-61]) - 1

    ex20s, ex60s, flows, heats = {}, {}, {}, {}
    for code in ind_close.columns:
        c = sub_close[code].dropna()
        if len(c) < 65:
            continue
        ex20s[code] = float(c.iloc[-1]) / float(c.iloc[-21]) - 1 - hs20
        ex60s[code] = float(c.iloc[-1]) / float(c.iloc[-61]) - 1 - hs60
        if code in sub_amt.columns:
            amt = sub_amt[code].dropna()
            if len(amt) >= 251:
                heats[code] = float((amt.tail(250) < amt.iloc[-1]).mean()) * 100
    total_amt = sub_amt.sum(axis=1)
    for code in ex20s:
        if code in sub_amt.columns:
            a, t = sub_amt[code].reindex(sub_amt.index).fillna(0), total_amt
            share = (a / t).dropna()
            if len(share) >= 6 and t.iloc[-1] > 0:
                flows[code] = float(share.iloc[-1] - share.iloc[-6]) * 1e4

    def _rank_pct(d_: Dict[str, float], cap: float) -> Dict[str, float]:
        if len(d_) < 2:
            return {}
        ordered = sorted(d_, key=lambda k: d_[k])
        return {k: i / (len(ordered) - 1) * cap for i, k in enumerate(ordered)}

    r20, r60, rf = _rank_pct(ex20s, W_EX20), _rank_pct(ex60s, W_EX60), _rank_pct(flows, W_FLOW)
    for code in ex20s:
        score = r20.get(code, 0) + r60.get(code, 0) + rf.get(code, 0) + heats.get(code, 0) / 100 * W_HEAT
        scores.append((score, code))
    scores.sort(reverse=True)
    return [c for _, c in scores[:TOP_N]]


def run(start: str) -> str:
    from src.etf.config import CORE_BASELINE, AssetType
    from src.market_state.style_state import fetch_industry_daily
    from data_provider import get_etf_daily, get_index_daily

    states = backtest_states(start)
    if len(states) < 10:
        return f"回放周数不足（{len(states)}），无法归因"
    fridays = [r["date"] for r in states]
    labels = [r["state"] for r in states]

    # ── 核心列：基准权重 × ETF 周收益 ──
    core_etfs = {a.code: a.neutral_weight for a in CORE_BASELINE if a.asset_type != AssetType.CASH}
    etf_close: Dict[str, pd.Series] = {}
    for code in core_etfs:
        df = get_etf_daily(code)
        if df is None or df.empty:
            logger.warning(f"核心 ETF {code} 日线缺失，按 0 收益处理")
            continue
        s = df.set_index("date")["close"].astype(float)
        s.index = s.index.astype(str)
        etf_close[code] = s
    w_sum = sum(core_etfs.values())
    core_weekly = []
    for a, b in zip(fridays, fridays[1:]):
        ret = 0.0
        for code, w in core_etfs.items():
            s = etf_close.get(code)
            if s is None:
                continue
            pa, pb = s.reindex([a, b]).tolist()
            if pd.notna(pa) and pd.notna(pb) and pa > 0:
                ret += (w / w_sum) * (float(pb) / float(pa) - 1)
        core_weekly.append(ret)

    # ── 动量列：行业指数代理 ──
    industries = fetch_industry_daily()  # {行业名: df(date, close, amount)}
    hs300 = get_index_daily(HS300, days=2500)
    hs300_close = hs300.set_index("date")["close"].astype(float)
    hs300_close.index = hs300_close.index.astype(str)

    ind_close = pd.DataFrame({name: df.set_index("date")["close"].astype(float)
                              for name, df in industries.items()})
    ind_close.index = ind_close.index.astype(str)
    ind_amount = pd.DataFrame({name: df.set_index("date")["amount"].astype(float)
                               for name, df in industries.items() if "amount" in df.columns})
    ind_amount.index = ind_amount.index.astype(str)

    mom_weekly = []
    for a, b in zip(fridays, fridays[1:]):
        picks = _momentum_picks(a, ind_close, ind_amount, hs300_close, {})
        if not picks:
            mom_weekly.append(None)
            continue
        rets = []
        for name in picks:
            c = ind_close[name].dropna()
            pa, pb = c.reindex([a, b]).tolist()
            if pd.notna(pa) and pd.notna(pb) and pa > 0:
                rets.append(float(pb) / float(pa) - 1)
        mom_weekly.append(sum(rets) / len(rets) if rets else None)

    # ── 按状态聚合 ──
    rows = []
    for i, b in enumerate(fridays[1:]):
        st, c, m = labels[i], core_weekly[i], mom_weekly[i]
        if c is None or m is None:
            continue
        rows.append({"date": b, "state": st, "core": c, "mom": m, "excess": m - c})

    def _fmt(v, pct=True):
        return f"{v * 100:+.2f}%" if pct else str(v)

    lines = [
        "# 📊 状态 × 策略归因（v0）",
        f"**区间**: {fridays[0]} → {fridays[-1]}（{len(rows)} 周）｜动量列=行业指数Top3代理，见脚本头折扣声明",
        "",
        "| 状态 | 周数 | 核心(均值) | 动量(均值) | 超额(均值) | 超额胜率 | 核心(中位) | 动量(中位) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    order = ["strong", "forming", "vacuum", "fading"]
    for st in order + [s for s in {r["state"] for r in rows} if s not in order]:
        grp = [r for r in rows if r["state"] == st]
        if not grp:
            continue
        n = len(grp)
        mc = sorted(r["core"] for r in grp)[n // 2]
        mm = sorted(r["mom"] for r in grp)[n // 2]
        exs = [r["excess"] for r in grp]
        lines.append(
            f"| {STATE_LABELS.get(st, st)} | {n} | {_fmt(sum(r['core'] for r in grp) / n)} "
            f"| {_fmt(sum(r['mom'] for r in grp) / n)} | {_fmt(sum(exs) / n)} "
            f"| {sum(1 for e in exs if e > 0) / n:.0%} | {_fmt(mc)} | {_fmt(mm)} |"
        )
    n = len(rows)
    exs = [r["excess"] for r in rows]
    lines.append(
        f"| **全部** | {n} | {_fmt(sum(r['core'] for r in rows) / n)} "
        f"| {_fmt(sum(r['mom'] for r in rows) / n)} | {_fmt(sum(exs) / n)} "
        f"| {sum(1 for e in exs if e > 0) / n:.0%} | — | — |"
    )
    lines.append("")
    lines.append("**解读门槛**：若各状态的动量超额分布无差异（均值/胜率接近），说明标签无区分度，")
    lines.append("阶段三（状态驱动资金分配）叫停；有差异才值得做精确回放。")
    lines.append("")

    # 逐周明细
    lines.append("## 逐周明细")
    lines.append("")
    lines.append("| 周 | 状态 | 核心 | 动量 | 超额 |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['date']} | {STATE_LABELS.get(r['state'], r['state'])} "
                     f"| {_fmt(r['core'])} | {_fmt(r['mom'])} | {_fmt(r['excess'])} |")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="状态 × 策略归因 v0（研究脚本，只读不交易）")
    parser.add_argument("--start", default="2021-01-01", help="回放起点（默认 2021-01-01）")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    setup_logging(log_prefix="state_attribution", debug=args.debug)
    report = run(args.start)

    if sys.stdout and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        except Exception:
            pass
    print(report)
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    out = os.path.join(reports_dir, f"state_attribution_{date.today().strftime('%Y%m%d')}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n已保存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
