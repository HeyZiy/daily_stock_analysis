# -*- coding: utf-8 -*-
"""
==================================
卫星预算门控模拟（v0，研究脚本）
==================================

回答 B 阶段实现方案选择：状态门控以什么力度落地？
- A 无条件持有：每周按动量分持有 Top3（跌出前 5 退出）——基线
- B 只禁新开：真空/退潮期不开新仓，存量按原规则（跌出前 5）自然退出
- C 全清仓：真空/退潮期直接清空卫星仓

核心度量：超额/周 与 持仓切换次数（换手代理）。
切换次数决定现实成本：off 段中位仅 1 周（详见分析），C 的换手可能吃光收益。

用法：
  python research/budget_gate_sim.py --start 2021-01-01
"""

import argparse
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.config import setup_env  # noqa: E402

setup_env()

from src.logging_config import setup_logging  # noqa: E402

setup_logging()

from src.etf.config import CORE_BASELINE, AssetType  # noqa: E402
from src.market_state.style_state import (  # noqa: E402
    S_FADING,
    S_VACUUM,
    fetch_index_daily,
    fetch_industry_daily,
)
from exit_signal_test import HS300, _replay_weekly  # noqa: E402

logger = __import__("logging").getLogger(__name__)

W_EX20, W_EX60, W_FLOW, W_HEAT = 30.0, 30.0, 20.0, 20.0
TOP_N, EXIT_RANK = 3, 5   # 持有 Top3，跌出前 5 退出（对齐实盘 RANK_EXIT_WINDOW）


def _scores_at(a: str, ind_close: pd.DataFrame, ind_amount: pd.DataFrame,
               hs300_close: pd.Series) -> Dict[str, float]:
    """与 _momentum_picks 同构的打分，但返回全行业分数（供 Top5 退出判定）。"""
    sub_close = ind_close[ind_close.index <= a]
    sub_amt = ind_amount[ind_amount.index <= a]
    if len(sub_close) < 65:
        return {}
    d = sub_close.index[-1]
    hs = hs300_close[hs300_close.index <= a]
    if len(hs) < 65:
        return {}
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
            a_, t = sub_amt[code].reindex(sub_amt.index).fillna(0), total_amt
            share = (a_ / t).dropna()
            if len(share) >= 6 and t.iloc[-1] > 0:
                flows[code] = float(share.iloc[-1] - share.iloc[-6]) * 1e4

    def rank_pct(d_: Dict[str, float], cap: float) -> Dict[str, float]:
        if len(d_) < 2:
            return {}
        ordered = sorted(d_, key=lambda k: d_[k])
        return {k: i / (len(ordered) - 1) * cap for i, k in enumerate(ordered)}

    r20, r60, rf = rank_pct(ex20s, W_EX20), rank_pct(ex60s, W_EX60), rank_pct(flows, W_FLOW)
    out = {}
    for code in ex20s:
        out[code] = r20.get(code, 0) + r60.get(code, 0) + rf.get(code, 0) + heats.get(code, 0) / 100 * W_HEAT
    return out


def _wk_ret(names: List[str], a: str, b: str, ind_close: pd.DataFrame) -> Optional[float]:
    rets = []
    for n in names:
        c = ind_close[n].dropna()
        pa, pb = c.reindex([a, b]).tolist()
        if pd.notna(pa) and pd.notna(pb) and pa > 0:
            rets.append(float(pb) / float(pa) - 1)
    return sum(rets) / len(rets) * 100 if rets else None


def run(start: str) -> str:
    from data_provider import get_etf_daily, get_index_daily

    industries = fetch_industry_daily()
    indices = fetch_index_daily()
    weekly = _replay_weekly(industries, indices, start)

    hs300 = get_index_daily(HS300, days=2500)
    hs300_close = hs300.set_index("date")["close"].astype(float)
    hs300_close.index = hs300_close.index.astype(str)
    ind_close = pd.DataFrame({n: df.set_index("date")["close"].astype(float)
                              for n, df in industries.items()})
    ind_close.index = ind_close.index.astype(str)
    ind_amount = pd.DataFrame({n: df.set_index("date")["amount"].astype(float)
                               for n, df in industries.items() if "amount" in df.columns})
    ind_amount.index = ind_amount.index.astype(str)

    core_etfs = {a.code: a.neutral_weight for a in CORE_BASELINE if a.asset_type != AssetType.CASH}
    etf_close = {}
    for code in core_etfs:
        df = get_etf_daily(code)
        if df is not None and not df.empty:
            s = df.set_index("date")["close"].astype(float)
            s.index = s.index.astype(str)
            etf_close[code] = s
    w_sum = sum(core_etfs.values())

    fridays = [w[0] for w in weekly]
    labels = [w[1] for w in weekly]

    # 策略口径：block_new=off 周禁新开；liquidate_streak=连续 N 个 off 周后清仓(0=立即)；
    # liquidate_states=触发清仓的状态集合（None=跟随 off 判定）
    POLICIES = {
        "A": {"block_new": False, "liquidate_streak": None},
        "B": {"block_new": True, "liquidate_streak": None},
        "C": {"block_new": True, "liquidate_streak": 0},
        "D": {"block_new": True, "liquidate_streak": 2},
        "E": {"block_new": True, "liquidate_streak": 0, "liquidate_states": (S_FADING,)},
    }
    NAMES = {"A": "A 无条件持有", "B": "B 只禁新开", "C": "C 立即清仓",
             "D": "D 连续2周off清仓", "E": "E 仅退潮清仓"}

    holdings = {k: [] for k in POLICIES}
    off_run = {k: 0 for k in POLICIES}
    rets = {k: [] for k in POLICIES}
    switches = {k: 0 for k in POLICIES}
    OFF_STATES = (S_VACUUM, S_FADING)

    for i in range(len(fridays) - 1):
        a, b = fridays[i], fridays[i + 1]
        scores = _scores_at(a, ind_close, ind_amount, hs300_close)
        if not scores:
            continue
        ranked = sorted(scores, key=scores.get, reverse=True)
        top5 = ranked[:EXIT_RANK]
        off_now = labels[i] in OFF_STATES

        core_ret = 0.0
        for code, w in core_etfs.items():
            s = etf_close.get(code)
            if s is None:
                continue
            pa, pb = s.reindex([a, b]).tolist()
            if pd.notna(pa) and pd.notna(pb) and pa > 0:
                core_ret += (w / w_sum) * (float(pb) / float(pa) - 1) * 100

        for k, cfg in POLICIES.items():
            off_run[k] = off_run[k] + 1 if off_now else 0
            block = cfg["block_new"] and off_now
            liq_states = cfg.get("liquidate_states") or OFF_STATES
            liquidate = (cfg["liquidate_streak"] is not None
                         and off_run[k] > cfg["liquidate_streak"]
                         and labels[i] in liq_states)

            held = [n for n in holdings[k] if n in top5]
            switches[k] += len(holdings[k]) - len(held)
            if liquidate:
                switches[k] += len(held)
                held = []
            new = [] if block else ranked[:TOP_N]
            switches[k] += len([n for n in new if n not in held])
            merged = held + [n for n in new if n not in held]
            holdings[k] = merged[:TOP_N]
            r = _wk_ret(holdings[k], a, b, ind_close) if holdings[k] else 0.0
            rets[k].append((r if r is not None else 0.0) - core_ret)

    years = len(fridays) / 52
    lines = [f"# 🚦 卫星预算门控模拟（{start} 起，{len(rets['A'])} 周）\n"]
    lines.append("| 口径 | 超额/周 | 年化超额 | 持仓切换次数 | 年均切换 |")
    lines.append("|---|---|---|---|---|")
    for k in ("A", "B", "C", "D", "E"):
        ex = rets[k]
        ann = (np.prod([1 + e / 100 for e in ex]) ** (52 / len(ex)) - 1) * 100
        lines.append(f"| {NAMES[k]} | {np.mean(ex):+.3f}% | {ann:+.1f}% | {switches[k]} | {switches[k] / years:.0f} |")
    lines.append("\n**口径**：Top3 持有、跌出前 5 退出（对齐实盘退出窗）；off=真空∪退潮；"
                 "D 用滞回（连续 2 个 off 周才清仓，回 on 立即恢复）；E 只对退潮清仓、真空仅禁新开。"
                 "切换次数为持仓层面买入+卖出事件数（换手代理，未计成本）。")
    lines.append("**解读**：C 若显著优于 B 且 D/E 不明显更差，则'立即清仓'稳健；"
                 "若 D 与 C 接近而切换更少，优先 D（抗状态抖动）。"
                 "真实策略还有门控/真口径因子差异，本模拟只回答'门控力度'这一件事。")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="卫星预算门控模拟")
    parser.add_argument("--start", default="2021-01-01", help="回放起始日")
    args = parser.parse_args()
    print(run(args.start))
