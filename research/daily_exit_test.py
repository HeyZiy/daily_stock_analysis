# -*- coding: utf-8 -*-
"""
==================================
周度建仓 × 日度退出实验（v0，研究脚本）
==================================

回答 exit_signal_test 的遗留问题：周度粒度的退出信号集体失灵，
改用日度粒度的止损/回落卖出能否挽回强势期末周损失。

方法（与 state_attribution / exit_signal_test 同框架，无未来函数）：
- 建仓不变：每周五按动量代理分选行业 Top3（标签为强势期的周才建仓）
- 持有期内逐日检查篮子净值（等权、日度再平衡近似）：
  - 止损卖出：净值 < 买入价 × (1 - 阈值) → 当日收盘卖出，现金到下周五
  - 回落卖出：净值 < 持有期峰值 × (1 - 阈值) → 同上（峰值锚定，让利润奔跑）
- 所有信号只用当日及之前收盘价；收盘触发按当日收盘成交
  （行业指数无开盘价，实盘按次日开盘执行会有小幅滑点差异，方向不系统性偏移）

阈值网格给出 2%/3%/5% 三档，看结论对阈值的稳健性，不挑单点最优。

用法：
  python research/daily_exit_test.py --start 2021-01-01
"""

import argparse
import os
import sys
from typing import Dict, List, Optional, Tuple

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
    S_STRONG,
    fetch_index_daily,
    fetch_industry_daily,
)
from exit_signal_test import HS300, TOP_N, _replay_weekly  # noqa: E402
from state_attribution import _momentum_picks  # noqa: E402

logger = __import__("logging").getLogger(__name__)

# 规则集：None=持有到期基线；(mode, thr) 中 mode: stop=止损(锚定买价), trail=回落(锚定峰值)
RULES = [
    ("hold", None, 0.0),
    ("stop2", "stop", 0.02),
    ("stop3", "stop", 0.03),
    ("stop5", "stop", 0.05),
    ("trail2", "trail", 0.02),
    ("trail3", "trail", 0.03),
    ("trail5", "trail", 0.05),
]


def _manage_path(path: pd.Series, mode: Optional[str], thr: float) -> Tuple[float, bool]:
    """对篮子日度净值路径应用退出规则。返回 (周收益%, 是否触发)。"""
    if mode is None or path.empty:
        v = float(path.iloc[-1]) if not path.empty else 1.0
        return (v - 1) * 100, False
    peak = 1.0
    for v in path:  # 逐日：先更新峰值再判断（当日新高后回落同日可比）
        v = float(v)
        peak = max(peak, v)
        if mode == "stop" and v <= 1 - thr:
            return (v - 1) * 100, True
        if mode == "trail" and v <= peak * (1 - thr):
            return (v - 1) * 100, True
    return (float(path.iloc[-1]) - 1) * 100, False


def run(start: str) -> str:
    from data_provider import get_etf_daily, get_index_daily

    industries = fetch_industry_daily()
    indices = fetch_index_daily()
    if len(industries) < 20:
        return f"行业日线仅 {len(industries)} 个，无法实验"

    weekly = _replay_weekly(industries, indices, start)
    if len(weekly) < 10:
        return f"回放周数不足（{len(weekly)}）"

    # 沪深300 走 get_index_daily(days=2500) 拿全历史（同 exit_signal_test 的教训）
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

    rows = []
    for i in range(len(fridays) - 1):
        a, b = fridays[i], fridays[i + 1]
        if labels[i] != S_STRONG:
            continue
        picks = _momentum_picks(a, ind_close, ind_amount, hs300_close, {})
        if not picks:
            continue

        # 篮子日度净值路径（等权、日度再平衡近似），锚定 a 收盘 = 1.0
        base = ind_close.loc[a, picks].astype(float)
        sub = ind_close.loc[(ind_close.index > a) & (ind_close.index <= b), picks]
        if sub.empty:
            continue
        path = sub.div(base).mean(axis=1).dropna()

        core_ret = 0.0
        for code, w in core_etfs.items():
            s = etf_close.get(code)
            if s is None:
                continue
            pa, pb = s.reindex([a, b]).tolist()
            if pd.notna(pa) and pd.notna(pb) and pa > 0:
                core_ret += (w / w_sum) * (float(pb) / float(pa) - 1) * 100

        rec = {"a": a, "b": b, "is_tail": labels[i + 1] == S_FADING, "core_ret": core_ret}
        for name, mode, thr in RULES:
            ret, fired = _manage_path(path, mode, thr)
            rec[name] = ret
            rec[name + "_fired"] = fired
        rows.append(rec)

    if len(rows) < 10:
        return f"有效强势期周不足（{len(rows)}）"

    tails = [r for r in rows if r["is_tail"]]
    goods = [r for r in rows if not r["is_tail"]]

    lines = []
    lines.append(f"# 🧪 周度建仓 × 日度退出实验（{rows[0]['a']} → {rows[-1]['b']}）\n")
    lines.append(f"强势期有效周 {len(rows)} 个（阶段末周 {len(tails)} 个）｜"
                 f"基线(hold)强势期超额 {np.mean([r['hold'] - r['core_ret'] for r in rows]):+.2f}%\n")
    lines.append("| 规则 | 触发/总周 | 末周捕获 | 末周挽回/次 | 好周误触发 | 好周放弃/次 | 强势期超额 | 胜率 |")
    lines.append("|---|---|---|---|---|---|---|---|")

    for name, mode, thr in RULES:
        label = {"hold": "持有到期"}.get(name, name)
        fired_t = [r for r in tails if r[name + "_fired"]]
        fired_g = [r for r in goods if r[name + "_fired"]]
        saved = np.mean([r[name] - r["hold"] for r in fired_t]) if fired_t else 0.0
        forgone = np.mean([r["hold"] - r[name] for r in fired_g]) if fired_g else 0.0
        ex = [r[name] - r["core_ret"] for r in rows]
        lines.append(
            f"| {label} | {sum(r[name + '_fired'] for r in rows)}/{len(rows)} "
            f"| {len(fired_t)}/{len(tails)} "
            f"| {saved:+.2f}% "
            f"| {len(fired_g)} ({len(fired_g) / max(len(goods), 1):.0%}) "
            f"| {forgone:+.2f}% "
            f"| {np.mean(ex):+.2f}% "
            f"| {sum(e > 0 for e in ex) / len(ex):.0%} |"
        )

    lines.append("\n**口径**：止损=净值较买价回撤超阈值；回落=净值较持有期峰值回撤超阈值；"
                 "触发当日收盘卖出、现金到下周五重新评估。")
    lines.append("**解读**：好规则应末周挽回大、好周放弃接近 0、结论在 2/3/5% 三档间方向一致；"
                 "若只在中档有效，多半是过拟合。实盘差异：按次日开盘成交、篮子非日度再平衡，"
                 "预期略差于本表。")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="周度建仓×日度退出实验")
    parser.add_argument("--start", default="2021-01-01", help="回放起始日")
    args = parser.parse_args()
    print(run(args.start))
