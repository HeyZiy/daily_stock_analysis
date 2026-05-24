# -*- coding: utf-8 -*-
"""
信号检测诊断脚本

对指定日期的自选股逐一运行信号检测，统计每个条件通过/失败情况。

用法：
    python diagnose_signals.py --date 2026-05-22
    python diagnose_signals.py --date 2026-05-22 --max-stocks 30
"""

import argparse
import logging
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import pandas as pd

from src.config import setup_env

setup_env()

logger = logging.getLogger("diagnose")


def _check_conditions(df: pd.DataFrame) -> Dict[str, object]:
    """不调用 detect_pullback_signals，独立计算所有条件值。"""
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None

    price = latest["close"]
    ma5 = latest["ma5"]
    ma10 = latest["ma10"]
    ma20 = latest["ma20"]

    if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
        return {"valid": False, "reason": "均线数据缺失"}

    bias_ma5 = (price - ma5) / ma5 * 100 if ma5 > 0 else 0
    pct_change = 0.0
    if prev is not None and prev["close"] > 0:
        pct_change = (price - prev["close"]) / prev["close"] * 100

    # 日内承接
    intraday_range = latest["high"] - latest["low"]
    if intraday_range > 0:
        cp = (latest["close"] - latest["low"]) / intraday_range
        us = (latest["high"] - max(latest["open"], latest["close"])) / intraday_range
        ls = (min(latest["open"], latest["close"]) - latest["low"]) / intraday_range
    else:
        cp, us, ls = 0.5, 0.0, 0.0

    # 换手率
    turnover = latest.get("turnover_rate", 0) or 0

    # 量能
    vol = latest["volume"]
    vol_ma5 = df["volume"].rolling(5).mean().iloc[-1] if len(df) >= 5 else 0

    # 情绪过热
    is_euphoric = False
    r3d = r5d = amp = 0.0
    if len(df) >= 4:
        r3d = (df.iloc[-1]["close"] - df.iloc[-4]["close"]) / df.iloc[-4]["close"] * 100
        r5d = (
            (df.iloc[-1]["close"] - df.iloc[-6]["close"]) / df.iloc[-6]["close"] * 100
            if len(df) >= 6
            else 0
        )
        r_bias = max(
            (df.iloc[i]["close"] - df.iloc[i]["ma5"]) / df.iloc[i]["ma5"] * 100
            for i in range(-min(5, len(df)), 0)
            if pd.notna(df.iloc[i]["ma5"]) and df.iloc[i]["ma5"] > 0
        ) if len(df) >= 2 else 0
        amp = max(
            (df.iloc[i]["high"] - df.iloc[i]["low"]) / df.iloc[i - 1]["close"] * 100
            for i in range(-3, 0)
            if df.iloc[i - 1]["close"] > 0
        )
        if r3d >= 18 or r5d >= 30 or r_bias >= 12 or amp >= 15:
            is_euphoric = True

    # 过去5天至少3天收盘在MA5之上
    recently_above_ma5 = (
        sum(1 for i in range(-6, -1) if df.iloc[i]["close"] > df.iloc[i]["ma5"]) >= 3
        if len(df) >= 6
        else False
    )

    # MA10 回踩
    touches_ma10 = latest["low"] <= ma10 * 1.01 and price >= ma10

    return {
        "valid": True,
        "price": price,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "bias_ma5": bias_ma5,
        "pct_change": pct_change,
        "turnover": turnover,
        "vol": vol,
        "vol_ma5": vol_ma5,
        "close_position": cp,
        "upper_shadow": us,
        "lower_shadow": ls,
        "r3d": r3d,
        "r5d": r5d,
        "amp": amp,
        # --- 逐条件判定 ---
        "bullish_alignment": ma5 > ma10 > ma20,
        "holds_ma5": price >= ma5 * 0.995,
        "meets_liquidity": turnover > 3.0,
        "no_volume_blowoff": vol_ma5 > 0 and vol < vol_ma5 * 1.1,
        "bias_ok": -1.5 < bias_ma5 < 3.5,
        "intraday_ok": cp > 0.4 and ls > 0.1,
        "not_euphoric": not is_euphoric,
        "recently_above_ma5": recently_above_ma5,
        "touches_ma10": touches_ma10,
    }


def main():
    parser = argparse.ArgumentParser(description="诊断信号检测条件")
    parser.add_argument("--date", type=str, default="2026-05-22", help="分析日期 YYYY-MM-DD")
    parser.add_argument("--max-stocks", type=int, default=50, help="最大分析数量")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    logger.info(f"诊断目标日期: {target_date}")

    # 延迟导入以先配置日志
    from src.logging_config import setup_logging

    setup_logging(log_prefix="diagnose_signals", debug=True)

    from data_provider.base import DataFetcherManager
    from src.services.mx_service import MXService
    from src.stock_analyzer import StockTrendAnalyzer

    fetcher = DataFetcherManager()
    mx_service = MXService()
    trend_analyzer = StockTrendAnalyzer()

    stock_codes, name_mapping = mx_service.fetch_self_selected()
    if not stock_codes:
        logger.error("没有获取到股票列表")
        return 1

    stock_codes = stock_codes[: args.max_stocks]
    logger.info(f"分析前 {len(stock_codes)} 只股票")

    # ---------- 统计计数器 ----------
    total = 0
    no_data = 0
    pre_filtered = 0  # 涨跌幅预过滤
    removed = 0
    no_bullish = 0
    entered = 0  # 进入信号检测（有多头排列）
    signal1_hit = 0
    signal2_hit = 0

    # 信号1 各条件失败计数（只统计进入信号检测的股票）
    s1_fails = Counter()
    s2_fails = Counter()

    # 记录每个条件失败的股票列表（用于深入排查）
    fail_details: Dict[str, list] = {k: [] for k in [
        "holds_ma5", "no_volume_blowoff", "bias_ok", "intraday_ok",
        "meets_liquidity", "not_euphoric", "recently_above_ma5"
    ]}

    for i, code in enumerate(stock_codes):
        name = name_mapping.get(code, code)
        try:
            start_str = (target_date - timedelta(days=60)).strftime("%Y-%m-%d")
            end_str = target_date.strftime("%Y-%m-%d")

            result = fetcher.get_daily_data(code, start_str, end_str)
            if isinstance(result, tuple):
                df = result[0]
            else:
                df = result

            if df is None or df.empty:
                no_data += 1
                continue

            df = df.sort_values("date").reset_index(drop=True)
            df["_dt"] = pd.to_datetime(df["date"]).dt.date
            df = df[df["_dt"] <= target_date]
            df = df.drop(columns=["_dt"])

            if len(df) < 20:
                no_data += 1
                continue

            df = trend_analyzer._calculate_mas(df)
            total += 1

            # 涨跌幅预过滤：只分析小涨小跌/横盘的股票（-3% ~ +5%）
            # 大涨日不是回踩形态，直接跳过
            latest_close = df.iloc[-1]["close"]
            prev_close = df.iloc[-2]["close"] if len(df) > 1 else latest_close
            day_pct = (latest_close - prev_close) / prev_close * 100 if prev_close > 0 else 0
            if day_pct < -3.0 or day_pct > 5.0:
                pre_filtered += 1
                logger.debug(f"  {name}({code}) 涨跌幅预过滤跳过 (pct={day_pct:+.2f}%)")
                continue

            # 剔除检查
            from src.strategy.removal_rules import check_removal_rules

            should_remove, reason = check_removal_rules(code, df)
            if should_remove:
                removed += 1
                logger.info(f"  ❌ 剔除 {name}({code}): {reason}")
                continue

            # 独立计算条件
            cond = _check_conditions(df)
            if not cond["valid"]:
                continue

            if not cond["bullish_alignment"]:
                no_bullish += 1
                logger.debug(
                    f"  {name}({code}) ✗ 非多头排列: "
                    f"ma5={cond['ma5']:.2f} ma10={cond['ma10']:.2f} ma20={cond['ma20']:.2f}"
                )
                continue

            entered += 1

            # --- 信号1 逐条件检查 ---
            s1_ok = True
            for cond_name in fail_details:
                if not cond[cond_name]:
                    s1_fails[cond_name] += 1
                    s1_ok = False
                    if len(fail_details[cond_name]) < 5:  # 每条件只保留前5个例子
                        fail_details[cond_name].append(
                            f"{name}({code}) bias={cond['bias_ma5']:+.2f}% "
                            f"turnover={cond['turnover']:.1f}% pct={cond['pct_change']:+.2f}%"
                        )

            if s1_ok:
                signal1_hit += 1
                logger.info(
                    f"  ✅ {name}({code}) 信号1命中! "
                    f"price={cond['price']:.2f} bias={cond['bias_ma5']:+.2f}% "
                    f"turnover={cond['turnover']:.1f}%"
                )

            # --- 信号2 逐条件检查 ---
            s2_cond = (
                cond["no_volume_blowoff"]
                and cond["touches_ma10"]
                and cond["intraday_ok"]
                and cond["meets_liquidity"]
                and cond["not_euphoric"]
                and not cond["holds_ma5"]
            )
            if s2_cond:
                signal2_hit += 1
                logger.info(
                    f"  ✅ {name}({code}) 信号2命中! "
                    f"price={cond['price']:.2f} bias={cond['bias_ma5']:+.2f}%"
                )
            else:
                if not cond["touches_ma10"]:
                    s2_fails["touches_ma10"] += 1
                if cond["holds_ma5"]:
                    s2_fails["still_holds_ma5"] += 1
                if not cond["no_volume_blowoff"]:
                    s2_fails["vol_blowoff"] += 1
                if not cond["intraday_ok"]:
                    s2_fails["intraday"] += 1
                if not cond["meets_liquidity"]:
                    s2_fails["liquidity"] += 1
                if not cond["not_euphoric"]:
                    s2_fails["euphoric"] += 1

            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i + 1}/{len(stock_codes)}")

        except Exception as e:
            logger.warning(f"分析 {name}({code}) 异常: {e}")
            continue

    # ==================== 汇总报告 ====================
    print("\n" + "=" * 65)
    print("  信号检测诊断报告")
    print("=" * 65)
    print(f"  目标日期: {target_date}")
    print(f"  股票总数: {len(stock_codes)}")
    print(f"  无数据/数据不足: {no_data}")
    print(f"  涨跌幅预过滤(-3~+5%): {pre_filtered}")
    print(f"  被剔除规则过滤: {removed}")
    print(f"  非多头排列:    {no_bullish}")
    print(f"  进入信号检测:  {entered}")
    print(f"  ────────────────────────")
    print(f"  信号1(pullback_ma5) 命中: {signal1_hit}")
    print(f"  信号2(pullback_ma10) 命中: {signal2_hit}")

    if entered > 0:
        print(f"\n  📊 信号1 各条件失败率 (共{entered}只进入检测):")
        for cond_name, label in [
            ("holds_ma5", "守住MA5"),
            ("no_volume_blowoff", "未放量"),
            ("bias_ok", "乖离率-1.5~3.5%"),
            ("intraday_ok", "日内承接"),
            ("meets_liquidity", "换手率>3%"),
            ("not_euphoric", "非情绪过热"),
            ("recently_above_ma5", "5天>=3天在MA5上"),
        ]:
            fail_count = s1_fails.get(cond_name, 0)
            pass_count = entered - fail_count
            bar = "█" * (pass_count * 20 // entered) + "░" * (fail_count * 20 // entered)
            print(f"    {label:　<12s}: 通过 {pass_count:>2d}/{entered} ({pass_count*100//entered:>2d}%) {bar}")

    if entered > 0:
        print(f"\n  🔍 信号1 失败示例（每条件最多5只）:")
        for cond_name, label in [
            ("holds_ma5", "守住MA5"),
            ("no_volume_blowoff", "未放量"),
            ("bias_ok", "乖离率-1.5~3.5%"),
            ("intraday_ok", "日内承接"),
            ("meets_liquidity", "换手率>3%"),
            ("not_euphoric", "非情绪过热"),
            ("recently_above_ma5", "5天>=3天在MA5上"),
        ]:
            examples = fail_details.get(cond_name, [])
            if examples:
                print(f"    --- {label} (失败{len(examples)}例) ---")
                for ex in examples:
                    print(f"      {ex}")

    print(f"\n  📊 信号2 各条件失败计数:")
    for k, label in [
        ("touches_ma10", "未触及MA10"),
        ("still_holds_ma5", "仍守住MA5(不触发信号2)"),
        ("vol_blowoff", "放量"),
        ("intraday", "日内承接弱"),
        ("liquidity", "换手率不足"),
        ("euphoric", "情绪过热"),
    ]:
        c = s2_fails.get(k, 0)
        print(f"    {label:　<20s}: {c}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
