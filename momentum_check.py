# -*- coding: utf-8 -*-
"""
==================================
行业动量轮动 — 单只诊断
==================================

对单只 ETF 跑卫星仓规则（口径与 src/etf/industry_momentum.py 完全一致），
只输出诊断，不拉持仓、不下单。

用法：
  python momentum_check.py 512400                  # 有色金属ETF南方（新浪日线）
  python momentum_check.py 512400 --name 有色金属   # 显式指定名称

说明：本工具仅诊断 ETF（卫星仓实际交易标的）。指数不在诊断范围——细分有色
000811 与跟踪它的 ETF（如 512400 有色金属ETF南方）并非同一篮子，要观察主题
热度请直接传对应 ETF 代码。日线统一走 data_provider.bars.get_etf_daily
（新浪单源 + 归一化），不再各自直连 akshare。
"""

import argparse
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd

from src.config import setup_env

setup_env()

from src.logging_config import setup_logging
from src.etf import industry_momentum as im
from data_provider import get_etf_daily, is_etf_code

logger = logging.getLogger(__name__)

# 指数/个股不在诊断范围；仅诊断 ETF（见 main 中 is_etf_code 校验）。
# 若需看主题热度，请直接传其跟踪 ETF（如 512400 有色金属ETF南方）。


def _lookup_etf_name(code: str) -> str:
    """从本地行业清单查名称（查不到返回空串）"""
    try:
        path = os.path.join("data", "etf_industry_map.json")
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        if isinstance(entries, list):
            for e in entries:
                if e.get("code") == code:
                    return e.get("name", "")
    except Exception:
        pass
    return ""


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def _recent_rows(res: dict, n: int) -> List[str]:
    df = res["df"]
    chg = (df["close"] / df["close"].shift(1) - 1) * 100
    vr5 = df["volume"] / df["vol_ma5"]
    vr20 = df["volume"] / df["vol_ma20"]
    rows = []
    for i in range(max(1, len(df) - n), len(df)):
        rows.append(
            f"  {df['date'].iloc[i]}  收盘 {df['close'].iloc[i]:>10.2f}"
            f"  涨幅 {chg.iloc[i]:+6.2f}%  量比5日 {vr5.iloc[i]:5.2f}  量比20日 {vr20.iloc[i]:5.2f}"
        )
    return rows


def _diagnosis(res: dict, is_index: bool, meta: dict, days: int) -> str:
    chg = res["chg_pct"]
    price_ok = res["new_high_60d"] or res["platform_break"]
    shape = "60日新高" if res["new_high_60d"] else ("20日箱体突破" if res["platform_break"] else "无新高/无箱体突破")
    chg_ok = im.BREAKOUT_CHANGE_MIN <= chg <= im.BREAKOUT_CHANGE_MAX
    vol5_ok = res["vol_ratio_5"] >= im.VOL_MULT_5D
    vol20_ok = res["vol_ratio_20"] >= im.VOL_MULT_20D

    L = ["=" * 56]
    src = "指数口径 · 中证官网" if is_index else "ETF口径 · 新浪日线"
    L.append(f"🚀 卫星仓单只诊断（行业动量轮动） — {res['name']}({res['code']})  [{src}]")
    L.append("=" * 56)
    L.append(f"最新数据日 {res['df']['date'].iloc[-1]} | 收盘 {res['close']:,.2f} | 当日涨幅 {chg:+.2f}%")
    L.append("")
    L.append(f"{_mark(price_ok)} 价能  {shape} | 20日涨幅 {res['ret_20d']:+.1f}%")
    L.append(f"{_mark(chg_ok)} 涨幅  {chg:+.2f}%（需 {im.BREAKOUT_CHANGE_MIN:.1f}% ~ {im.BREAKOUT_CHANGE_MAX:.1f}%）")
    L.append(f"{_mark(vol5_ok)} 量能  量比5日 {res['vol_ratio_5']:.2f}（需 ≥{im.VOL_MULT_5D:.1f}）")
    L.append(f"{_mark(vol20_ok)} 量能  量比20日 {res['vol_ratio_20']:.2f}（需 ≥{im.VOL_MULT_20D:.1f}）")
    if res["pe_pct"] is not None:
        L.append(f"{_mark(res['pe_pct'] < im.PE_LOCK)} 估值  行业PE分位 {res['pe_pct']:.0f}%（≥{im.PE_LOCK:.0f}% 锁仓）")
    else:
        pe_extra = f" | 中证滚动PE {meta['pe']:.1f}" if meta.get("pe") else ""
        L.append(f"⬜ 估值  无行业PE分位映射（不硬剔，仅少评分）{pe_extra}")
    L.append("")
    if res.get("momentum_rank") is not None:
        L.append(f"行业动量 第{res['momentum_rank']}名 / 动量分 {res['momentum_score']}"
                 f"（关注池={'是' if res.get('pool') else '否'}）"
                 + (f" | 超额20日 {res['excess_20d']:+.1f}% / 60日 {res['excess_60d']:+.1f}%"
                    if res.get("excess_20d") is not None else ""))
    verdict = "🔥 触发放量突破" if res["breakout"] else "未触发放量突破"
    if res["breakout"] and res.get("momentum_rank") is not None and not res.get("pool", False):
        verdict += "（但不在动量关注池，实战不入选）"
    L.append(f"判定: {verdict}")
    L.append("")
    L.append(f"近{days}日明细:")
    L.extend(_recent_rows(res, days))
    L.append("")
    L.append("*诊断口径与卫星仓引擎一致（突破触发层）；正式买卖由 etf_observe.py 周度批次决定。")
    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser(description="卫星仓单只诊断（行业动量轮动口径，只诊断不交易）")
    parser.add_argument("code", help="6 位代码（000/399 开头按中证/国证指数处理，其余按 ETF）")
    parser.add_argument("--name", default="", help="标的名称（缺省自动取指数简称/行业清单名）")
    parser.add_argument("--days", type=int, default=10, help="近 N 日明细行数（默认 10）")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    setup_logging(log_prefix="momentum_check", debug=args.debug)

    code = args.code.strip()
    if not is_etf_code(code):
        print(f"{code} 不是 ETF。只诊断卫星仓 ETF 标的（如 512400 有色金属ETF南方）。")
        print(f"指数/个股不在诊断范围：细分有色 000811 与 512400 并非同一篮子，请直接传 ETF 代码。")
        return 1

    injected_df = get_etf_daily(code)
    if injected_df is None:
        print(f"{code} 日线获取失败，无法诊断")
        return 1
    logger.info(f"ETF日线 {len(injected_df)} 根")

    name = args.name or _lookup_etf_name(code) or code
    res = im.analyze_etf(code, name, df=injected_df)
    if res is None:
        print(f"{name}({code}) 数据不足（需 ≥65 根日线），无法诊断")
        return 1
    # 附加行业动量层（截面排名需要全清单对比，单标的诊断给参考值）
    res = im._attach_momentum_scores([res])[0]
    res["pe_pct"] = im._pe_pct_for(res)

    if sys.stdout and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        except Exception:
            pass
    print(_diagnosis(res, False, {}, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
