# -*- coding: utf-8 -*-
"""
==================================
强势期动量仓·早期退出信号实验（v0，研究脚本）
==================================

回答 state_attribution 发现的问题：状态机确认退潮有滞后，主线崩盘周
（阶段末周，均值超额 -3.43%）仍挂在强势期标签下，把强势期动量超额
从 +0.93% 拖到 +0.09%。本实验测：在期初（周五 a）能否用更快的信号
提前降仓，避免这批周。

方法（与 state_attribution 同框架，无未来函数）：
- 逐周五重放状态机取标签；动量腿 = 行业指数 Top3 代理（同构打分）
- 仅对标签为强势期的周（a→b）测试退出规则：信号用 as_of=a 及之前数据
  计算，触发则该周动量腿记 0（转现金持有一周），下周恢复正常评估
- 指标：末周捕获率（下一周标签为退潮期的周被触发的比例）、
  误触发率及其放弃的收益、强势期/全样本动量超额均值的变化

信号清单（全部为周五 a 收盘后可算）：
- S1 持仓回撤：动量组合上周收益 < -2.5%
- S2 领涨组滞涨：当期 Top5 的 5 日均值收益 < 0
- S3 重合率塌陷：领涨组重合率 ≤ 0.2（Top5 只守住 1 个）
- S4 大盘走弱：沪深300 收盘 < MA20
- S5 宽度恶化：行业 MA20 上方占比 < 0.45
- C1 = S2|S3（主线自身恶化）；C2 = 任一信号触发

已知折扣（同 state_attribution）：动量腿是代理、无交易成本、
周度粒度（实盘信号可日内触发，本实验只测周度可决策性）。

用法：
  python research/exit_signal_test.py --start 2021-01-01
"""

import argparse
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import setup_env  # noqa: E402

setup_env()

from src.logging_config import setup_logging  # noqa: E402

setup_logging()

from src.etf.config import CORE_BASELINE, AssetType  # noqa: E402
from src.market_state.style_state import (  # noqa: E402
    S_FADING,
    S_STRONG,
    STATE_LABELS,
    classify,
    compute_snapshot,
    fetch_index_daily,
    fetch_industry_daily,
    update_state,
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state_attribution import _momentum_picks  # noqa: E402

logger = __import__("logging").getLogger(__name__)

HS300 = "000300"
TOP_N = 3
S1_DRAWDOWN = -2.5   # 持仓回撤触发阈值（%）
S3_RETENTION = 0.2   # 重合率塌陷阈值
S5_BREADTH = 0.45    # 宽度恶化阈值


def _replay_weekly(industries, indices, start: str):
    """逐周五重放状态机（同 backtest_states 口径），返回 [(周五str, 状态码, snapshot)]。"""
    ref = industries.get("银行")
    if ref is None:
        ref = next(iter(industries.values()))
    ref = ref[ref["date"] >= pd.Timestamp(start)]
    week_keys = ref["date"].dt.strftime("%G-%V")
    eval_dates = ref.groupby(week_keys)["date"].max().tolist()

    prev = {"state": None, "state_streak": 0, "strong_cand_streak": 0,
            "vacuum_cand_streak": 0, "prev_top5": []}
    out = []
    for d in eval_dates:
        snap = compute_snapshot(industries, indices, prev_top5=prev["prev_top5"], as_of=d.date())
        if snap is None:
            continue
        state, streaks, _ = classify(snap, prev)
        prev = update_state(snap, state, streaks, prev)
        out.append((d.strftime("%Y-%m-%d"), state, snap))
    return out


def _build_rows(industries, indices, start: str) -> List[dict]:
    """逐周构建：动量收益、核心收益、信号触发。供 run 与诊断复用。"""
    from data_provider import get_etf_daily, get_index_daily

    weekly = _replay_weekly(industries, indices, start)
    if len(weekly) < 10:
        return []

    # 动量腿与核心腿的数据底座（同 state_attribution）
    # 注意：沪深300 必须走 get_index_daily(days=2500) 拿全历史，
    # style_state.fetch_index_daily 的沪深300只有约两年深度，会导致早期周全部静默无 picks
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

    fridays = [w[0] for w in weekly]
    labels = [w[1] for w in weekly]
    snaps = {w[0]: w[2] for w in weekly}

    # 逐周计算：动量收益、核心收益、信号
    rows = []
    prev_mom_ret: Optional[float] = None
    for i in range(len(fridays) - 1):
        a, b = fridays[i], fridays[i + 1]
        label_a = labels[i]

        picks = _momentum_picks(a, ind_close, ind_amount, hs300_close, {})
        mom_ret = None
        if picks:
            rets = []
            for name in picks:
                c = ind_close[name].dropna()
                pa, pb = c.reindex([a, b]).tolist()
                if pd.notna(pa) and pd.notna(pb) and pa > 0:
                    rets.append(float(pb) / float(pa) - 1)
            mom_ret = sum(rets) / len(rets) * 100 if rets else None

        core_ret = 0.0
        for code, w in core_etfs.items():
            s = etf_close.get(code)
            if s is None:
                continue
            pa, pb = s.reindex([a, b]).tolist()
            if pd.notna(pa) and pd.notna(pb) and pa > 0:
                core_ret += (w / sum(core_etfs.values())) * (float(pb) / float(pa) - 1) * 100

        snap = snaps[a]
        hs = hs300_close[hs300_close.index <= a]
        below_ma20 = bool(len(hs) >= 20 and float(hs.iloc[-1]) < float(hs.tail(20).mean()))

        signals = {
            "S1": prev_mom_ret is not None and prev_mom_ret < S1_DRAWDOWN,
            "S2": snap["top5_ret5"] < 0,
            "S3": snap["retention"] is not None and snap["retention"] <= S3_RETENTION,
            "S4": below_ma20,
            "S5": snap["breadth"] < S5_BREADTH,
        }
        signals["C1"] = signals["S2"] or signals["S3"]
        signals["C2"] = any(signals[s] for s in ("S1", "S2", "S3", "S4", "S5"))

        rows.append({
            "a": a, "b": b, "label": label_a,
            "is_tail": label_a == S_STRONG and labels[i + 1] == S_FADING,  # 诊断用（含未来信息）
            "mom_ret": mom_ret, "core_ret": core_ret,
            **signals,
        })
        prev_mom_ret = mom_ret

    return rows


def run(start: str) -> str:
    from src.market_state.style_state import fetch_industry_daily

    industries = fetch_industry_daily()
    indices = fetch_index_daily()
    if len(industries) < 20:
        return f"行业日线仅 {len(industries)} 个，无法实验"

    rows = _build_rows(industries, indices, start)
    if not rows:
        return "回放周数不足"

    strong = [r for r in rows if r["label"] == S_STRONG and r["mom_ret"] is not None]
    tails = [r for r in strong if r["is_tail"]]
    goods = [r for r in strong if not r["is_tail"]]
    all_valid = [r for r in rows if r["mom_ret"] is not None]

    def phase_stats(rs, rule):
        """应用退出规则后的动量腿/超额统计。rule=None 为基线。
        规则只在强势期周生效（r['label']==S_STRONG），其他状态原样持有。"""
        mom = [(0.0 if (rule and r[rule] and r["label"] == S_STRONG) else r["mom_ret"])
               for r in rs]
        core = [r["core_ret"] for r in rs]
        ex = [m - c for m, c in zip(mom, core)]
        return (np.mean(mom), np.mean(ex), sum(e > 0 for e in ex) / len(ex), len(rs))

    lines = []
    lines.append(f"# 🧪 强势期退出信号实验（{start} → {rows[-1]['b']}）\n")
    lines.append(f"强势期有效周 {len(strong)} 个（其中阶段末周 {len(tails)} 个）\n")
    base_strong = phase_stats(strong, None)
    lines.append(f"**基线**：强势期动量均值 {base_strong[0]:+.2f}%，超额 {base_strong[1]:+.2f}%，胜率 {base_strong[2]:.0%}\n")
    lines.append("| 规则 | 触发/强势周 | 末周捕获 | 误触发(好周) | 好周放弃收益/次 | 强势期超额 | 全样本超额 |")
    lines.append("|---|---|---|---|---|---|---|")

    base_all = phase_stats(all_valid, None)
    for rule in ("S1", "S2", "S3", "S4", "S5", "C1", "C2"):
        fired_tail = [r for r in tails if r[rule]]
        fired_good = [r for r in goods if r[rule]]
        give_up = np.mean([r["mom_ret"] - r["core_ret"] for r in fired_good]) if fired_good else 0.0
        s_strong = phase_stats(strong, rule)
        s_all = phase_stats(all_valid, rule)
        lines.append(
            f"| {rule} | {len(fired_tail) + len(fired_good)}/{len(strong)} "
            f"| {len(fired_tail)}/{len(tails)} "
            f"| {len(fired_good)} ({len(fired_good) / max(len(goods), 1):.0%}) "
            f"| {give_up:+.2f}% "
            f"| {s_strong[1]:+.2f}% ({s_strong[2]:.0%}) "
            f"| {s_all[1]:+.2f}% ({s_all[2]:.0%}) |"
        )

    lines.append("\n**规则说明**：S1 持仓上周回撤<-2.5%｜S2 Top5五日均值<0｜S3 重合率≤0.2｜"
                 "S4 沪深300<MA20｜S5 行业MA20上方占比<0.45｜C1=S2∪S3｜C2=全部任一")
    lines.append("**解读**：好的规则应末周捕获高、误触发少且好周放弃收益接近 0；"
                 "强势期超额显著改善且全样本不恶化才算有用。末周捕获用到了下周标签（诊断视角），"
                 "实盘不可知，但用于评估信号时效是合法的。")

    # 最优组合的逐周明细（触发周一览）
    best = max(("S1", "S2", "S3", "S4", "S5", "C1", "C2"),
               key=lambda r: phase_stats(strong, r)[1])
    fired_all = [r for r in strong if r[best]]
    lines.append(f"\n## {best} 触发周明细（强势期内共 {len(fired_all)} 次）\n")
    lines.append("| 周初a→b | 末周? | 动量收益(放弃/避免) |")
    lines.append("|---|---|---|")
    for r in fired_all:
        lines.append(f"| {r['a']}→{r['b']} | {'是' if r['is_tail'] else '否'} | {r['mom_ret']:+.2f}% |")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="强势期退出信号实验")
    parser.add_argument("--start", default="2021-01-01", help="回放起始日")
    args = parser.parse_args()
    print(run(args.start))
