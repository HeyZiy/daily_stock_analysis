# -*- coding: utf-8 -*-
"""
===================================
ETF 火箭 — 卫星仓量价突破引擎
===================================

主账户卫星仓的战术策略：在行业 ETF 清单（ETF_INDUSTRY_MAP）中捕捉
放量突破首日（主升浪启动第一棒），快进快出。

三要素共振：
  1. 价能 —— 60 日新高或 20 日箱体平台突破，当日涨幅 3% ~ 9.5%
  2. 量能 —— 当日量 ≥ 5 日均量 × 2 且 ≥ 20 日均量 × 1.5
  3. 性价比 —— 行业 PE 分位 < 60%（软条件），风险回报比 ≥ 2（结构保证）

退出（条件满足其一）：
  - 止损：价格 < 买入价 × 0.93
  - 止盈：+15% 减半，+25% 清仓
  - 动能衰竭：连续 3 日收盘 < MA5
  - 时间止损：10 个交易日无新高
  - 环境锁仓：PE 分位 ≥ 60% 或市场门控硬拦截 → 全清

仓位：总资产 10% 预算，最多 2 只，等权。

无状态设计：持仓起点不从本地文件记，全部以模拟仓为事实来源——
成本价用持仓接口的 cost_price，买入日期用历史委托接口（get_last_buy_dates）
最近一笔买入成交日期推导；查不到时降级为跳过日期类退出规则。
"""

import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

SATELLITE_BUDGET_RATIO = 0.10   # 卫星仓总仓位上限（总资产占比）
MAX_HOLDINGS = 2                # 最多持仓数
BREAKOUT_CHANGE_MIN = 3.0       # 突破日涨幅下限（%）
BREAKOUT_CHANGE_MAX = 9.5       # 突破日涨幅上限（%，排除一字板/近涨停）
VOL_MULT_5D = 2.0               # 量能：≥ 5 日均量倍数
VOL_MULT_20D = 1.5              # 量能：≥ 20 日均量倍数
SCORE_THRESHOLD = 70            # 火箭评分入围线（满分约 95）
PE_LOCK = 60.0                  # 行业 PE 分位锁仓线
STOP_LOSS = 0.93                # 止损线（买入价 × 0.93）
TAKE_PROFIT_HALF = 1.15         # 止盈减半线
TAKE_PROFIT_ALL = 1.25          # 止盈清仓线
MA5_EXIT_DAYS = 3               # 连续 N 日收盘 < MA5 退出
TIME_STOP_DAYS = 10             # N 个交易日无新高退出
PLATFORM_AMPLITUDE = 0.25       # 箱体定义：近 20 日振幅上限


# ── 单 ETF 分析 ──

def analyze_etf(code: str, name: str = "") -> Optional[dict]:
    """拉取单只 ETF 日线并计算火箭信号 + 动量观察分。失败返回 None。"""
    from src.etf.sector_rotation import _fetch_etf_daily

    df = _fetch_etf_daily(code)
    if df is None or len(df) < 65:
        return None

    close = df["close"]
    vol = df["volume"]
    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["vol_ma5"] = vol.rolling(5).mean()
    df["vol_ma20"] = vol.rolling(20).mean()

    latest = df.iloc[-1]
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else float(latest["close"])
    chg_pct = (float(latest["close"]) / prev_close - 1) * 100

    vol_ratio_5 = float(latest["vol_ma5"]) and float(latest["volume"]) / float(latest["vol_ma5"])
    vol_ratio_20 = float(latest["vol_ma20"]) and float(latest["volume"]) / float(latest["vol_ma20"])

    high_60 = float(close.iloc[-60:].max())
    high_20, low_20 = float(close.iloc[-20:].max()), float(close.iloc[-20:].min())
    new_high_60d = float(latest["close"]) >= high_60 * 0.995
    amplitude_20 = (high_20 - low_20) / low_20 if low_20 > 0 else 1.0
    platform_break = amplitude_20 < PLATFORM_AMPLITUDE and float(latest["close"]) > high_20 * 0.99

    day_high = float(latest.get("high", latest["close"]))
    day_low = float(latest.get("low", latest["close"]))
    day_open = float(latest.get("open", latest["close"]))
    body_ratio = (float(latest["close"]) - day_open) / (day_high - day_low) if day_high > day_low else 1.0

    ret_20d = (float(latest["close"]) / float(close.iloc[-21]) - 1) * 100
    ret_5d = (float(latest["close"]) / float(close.iloc[-6]) - 1) * 100

    # 行业估值（AmazingData，失败不影响）
    pe_pct = None
    try:
        from src.etf.amazing_factors import (
            get_etf_industry, get_industry_code_by_name, get_industry_pe_percentile,
        )
        industry = get_etf_industry(code)
        if industry:
            ind_code = get_industry_code_by_name(industry)
            if ind_code:
                pe_info = get_industry_pe_percentile(ind_code)
                if pe_info:
                    pe_pct = pe_info["pe_pct"]
    except Exception:
        pass

    # ── 火箭评分（量能 35 + 价能 35 + 性价比 ≤25）──
    vol_mult = min(vol_ratio_5, vol_ratio_20)
    if vol_mult >= 4:
        score_vol = 35
    elif vol_mult >= 3:
        score_vol = 25
    elif vol_mult >= 2:
        score_vol = 15
    else:
        score_vol = 0

    score_price = 0
    if new_high_60d:
        score_price = 20
    elif platform_break:
        score_price = 15
    if body_ratio > 0.7:
        score_price += 15
    elif body_ratio > 0.5:
        score_price += 10

    pe_score = 0.0
    if pe_pct is not None:
        if pe_pct < 20:
            pe_score = 15
        elif pe_pct < 40:
            pe_score = 10
        elif pe_pct < 60:
            pe_score = 5
    score_value = 10 + pe_score  # 风险回报比结构保证 ≥2 → 固定 10 分

    rocket_score = round(score_vol + score_price + score_value)

    # ── 动量观察分（行业轮动排名用，口径同 sector_rotation）──
    if ret_20d > 10:
        s20 = 30
    elif ret_20d > 5:
        s20 = 20 + (ret_20d - 5) * 2
    elif ret_20d > 0:
        s20 = 5 + ret_20d * 3
    elif ret_20d > -5:
        s20 = max(0, 5 + ret_20d)
    else:
        s20 = 0

    if ret_5d > 5:
        s5 = 25
    elif ret_5d > 2:
        s5 = 15 + (ret_5d - 2) * 3.3
    elif ret_5d > 0:
        s5 = 5 + ret_5d * 5
    elif ret_5d > -3:
        s5 = max(0, 5 + ret_5d * 1.6)
    else:
        s5 = 0

    ma5, ma10, ma20v = latest["ma5"], latest["ma10"], latest["ma20"]
    if pd.notna(ma5) and pd.notna(ma10) and pd.notna(ma20v):
        if ma5 > ma10 > ma20v:
            s_ma = 25
        elif ma5 > ma10:
            s_ma = 12
        elif latest["close"] > ma20v:
            s_ma = 5
        else:
            s_ma = 0
    else:
        s_ma = 0

    v5, v20 = latest["vol_ma5"], latest["vol_ma20"]
    if pd.notna(v5) and pd.notna(v20) and v20 > 0:
        ratio = v5 / v20
        if ratio > 1.5:
            s_vol = 20
        elif ratio > 1.0:
            s_vol = 10 + (ratio - 1.0) * 20
        elif ratio > 0.8:
            s_vol = 5 + (ratio - 0.8) * 25
        else:
            s_vol = 0
    else:
        s_vol = 0

    rot_score = round(s20 + s5 + s_ma + s_vol)

    # 连续 3 日收盘 < MA5
    last3 = df.tail(3)
    below_ma5_3d = bool((last3["close"] < last3["ma5"]).all()) if len(last3) == 3 else False

    breakout = (
        (new_high_60d or platform_break)
        and BREAKOUT_CHANGE_MIN <= chg_pct <= BREAKOUT_CHANGE_MAX
        and vol_ratio_5 >= VOL_MULT_5D
        and vol_ratio_20 >= VOL_MULT_20D
    )

    return {
        "code": code, "name": name, "df": df,
        "close": float(latest["close"]), "chg_pct": round(chg_pct, 1),
        "vol_ratio_5": round(vol_ratio_5, 2), "vol_ratio_20": round(vol_ratio_20, 2),
        "new_high_60d": new_high_60d, "platform_break": platform_break,
        "body_ratio": round(body_ratio, 2),
        "ret_20d": round(ret_20d, 1), "ret_5d": round(ret_5d, 1),
        "pe_pct": pe_pct, "rocket_score": rocket_score, "rot_score": rot_score,
        "breakout": breakout, "below_ma5_3d": below_ma5_3d,
    }


def analyze_universe() -> List[dict]:
    """分析全行业 ETF 清单（火箭信号 + 动量观察分，一份数据两用）。"""
    from src.etf.amazing_factors import get_industry_etf_universe

    results = []
    for entry in get_industry_etf_universe():
        res = analyze_etf(entry["code"], entry["name"])
        if res is not None:
            results.append(res)
    return results


# ── 决策 ──

def check_pe_lock(pe_pct: Optional[float]) -> bool:
    """PE 分位 ≥ 60% → 卫星仓锁仓。数据缺失时视为未锁。"""
    return pe_pct is not None and pe_pct >= PE_LOCK


def _entry_low_from_kline(res: dict, entry_date: str) -> float:
    """按买入日期从日线查买入日最低价（查不到返回 0）。"""
    try:
        df = res["df"]
        rows = df[df["date"].astype(str) == entry_date]
        if not rows.empty and "low" in rows.columns:
            return float(rows.iloc[-1]["low"])
    except Exception:
        pass
    return 0.0


def build_sell_orders(results: List[dict], positions: List[dict], entry_map: Dict[str, str],
                      pe_pct: Optional[float], hard_intercept: bool) -> Tuple[List[dict], List[str]]:
    """生成卫星卖出订单（按退出规则逐个检查，持仓起点全部来自模拟仓数据）。"""
    from src.etf.sector_rotation import RotationOrder

    result_map = {r["code"]: r for r in results}
    universe_codes = set(result_map)

    orders: List[RotationOrder] = []
    reasons: List[str] = []
    locked = check_pe_lock(pe_pct) or hard_intercept

    for p in positions:
        code = p.get("code", "")
        if code not in universe_codes or int(p.get("count", 0) or 0) <= 0:
            continue
        entry_price = float(p.get("cost_price", 0) or 0)
        entry_date = entry_map.get(code, "")
        entry_low = _entry_low_from_kline(result_map[code], entry_date) if entry_date else 0.0
        price = float(p.get("current_price", 0) or 0)
        count = int(p.get("count", 0) or 0)
        name = p.get("name", "") or code
        res = result_map[code]

        if locked:
            reason = "PE 分位 ≥60% 锁仓" if not hard_intercept else "市场门控硬拦截"
            orders.append(RotationOrder(code=code, name=name, action="sell", shares=count,
                                        price=price, amount=count * price, reason=reason))
            reasons.append(f"{name}({code}) {reason} 清仓")
            continue

        # 1. 止损：现价跌破持仓成本 × 0.93 或买入日最低价（仅在有起点数据时判定）
        if entry_price > 0 and price < entry_price * STOP_LOSS:
            orders.append(RotationOrder(code=code, name=name, action="sell", shares=count,
                                        price=price, amount=count * price, reason=f"止损（低于成本×{STOP_LOSS}）"))
            reasons.append(f"{name}({code}) 止损清仓")
            continue
        if entry_low > 0 and price < entry_low:
            orders.append(RotationOrder(code=code, name=name, action="sell", shares=count,
                                        price=price, amount=count * price, reason="跌破买入日最低价"))
            reasons.append(f"{name}({code}) 跌破买入日最低价 清仓")
            continue

        # 2. 止盈（相对持仓成本）
        if entry_price > 0:
            if price >= entry_price * TAKE_PROFIT_ALL:
                orders.append(RotationOrder(code=code, name=name, action="sell", shares=count,
                                            price=price, amount=count * price, reason="止盈 +25% 清仓"))
                reasons.append(f"{name}({code}) 止盈清仓")
                continue
            if price >= entry_price * TAKE_PROFIT_HALF and count >= 200:
                half = int(count // 2 // 100) * 100
                orders.append(RotationOrder(code=code, name=name, action="sell", shares=half,
                                            price=price, amount=half * price, reason="止盈 +15% 减半"))
                reasons.append(f"{name}({code}) 止盈减半 {half}股")
                continue

        # 3. 动能衰竭：连续 3 日收盘 < MA5
        if res.get("below_ma5_3d"):
            orders.append(RotationOrder(code=code, name=name, action="sell", shares=count,
                                        price=price, amount=count * price, reason=f"连续{MA5_EXIT_DAYS}日收盘<MA5"))
            reasons.append(f"{name}({code}) 动能衰竭清仓")
            continue

        # 4. 时间止损：10 个交易日无新高（需买入日期起点）
        if entry_price > 0 and entry_date:
            df = res["df"]
            try:
                since = df[df["date"].astype(str) >= entry_date]
            except Exception:
                since = df.tail(TIME_STOP_DAYS)
            if len(since) >= TIME_STOP_DAYS and float(since["high"].max()) < entry_price * 1.001:
                orders.append(RotationOrder(code=code, name=name, action="sell", shares=count,
                                            price=price, amount=count * price, reason=f"{TIME_STOP_DAYS}个交易日无新高"))
                reasons.append(f"{name}({code}) 时间止损退出")

    return orders, reasons


def build_buy_orders(results: List[dict], held_codes: set, total_assets: float,
                     satellite_mv: float, pe_pct: Optional[float], hard_intercept: bool,
                     regime: str) -> Tuple[List[dict], List[str]]:
    """生成卫星买入订单（突破信号 + 环境许可 + 预算内排名前 N）。"""
    from src.etf.sector_rotation import RotationOrder

    if hard_intercept or check_pe_lock(pe_pct):
        return [], []
    if regime not in ("trending_up", "weak_up"):
        return [], []

    budget = total_assets * SATELLITE_BUDGET_RATIO - satellite_mv
    if budget <= 0:
        return [], []

    candidates = [r for r in results
                  if r["breakout"] and r["rocket_score"] >= SCORE_THRESHOLD
                  and r["code"] not in held_codes
                  and (r["pe_pct"] is None or r["pe_pct"] < PE_LOCK)]
    candidates.sort(key=lambda r: r["rocket_score"], reverse=True)

    slots = max(0, MAX_HOLDINGS - len(held_codes))
    selected = candidates[:slots]
    if not selected:
        return [], []

    per_position = budget / len(selected)
    orders, notes = [], []
    for r in selected:
        price = r["close"]
        if price <= 0:
            continue
        shares = max(100, int(per_position / price / 100) * 100)
        orders.append(RotationOrder(code=r["code"], name=r["name"], action="buy",
                                    shares=shares, price=price, amount=shares * price,
                                    reason=f"放量突破，火箭评分 {r['rocket_score']}"))
        notes.append(f"{r['name']}({r['code']}) 突破信号 评分{r['rocket_score']} 20日{r['ret_20d']:+.1f}%")

    return orders, notes


# ── 汇总 ──

def analyze_satellite(positions: List[dict], total_assets: float,
                      pe_pct: Optional[float], hard_intercept: bool, regime: str,
                      client=None) -> dict:
    """卫星仓全流程：分析 → 卖出 → 买入。不执行交易，只出指令。

    持仓起点（成本/买入日）全部来自模拟仓接口，无本地状态文件：
    - 成本价：positions 的 cost_price
    - 买入日期：client.get_last_buy_dates() 推导（查不到则跳过日期类退出规则）
    """
    results = analyze_universe()

    entry_map: Dict[str, str] = {}
    if client is not None:
        try:
            entry_map = client.get_last_buy_dates()
        except Exception:
            logger.warning("历史委托查询失败，日期类退出规则降级跳过", exc_info=True)

    sells, sell_notes = build_sell_orders(results, positions, entry_map, pe_pct, hard_intercept)

    universe_codes = {r["code"] for r in results}
    satellite_mv = sum(float(p.get("market_value", 0) or 0)
                       for p in positions if p.get("code", "") in universe_codes)
    held_codes = {p.get("code") for p in positions
                  if p.get("code", "") in universe_codes and int(p.get("count", 0) or 0) > 0}

    buys, buy_notes = build_buy_orders(results, held_codes, total_assets, satellite_mv,
                                       pe_pct, hard_intercept, regime)

    # 行业轮动观察排名（同一份分析结果，只输出不动手）
    ranked = sorted([r for r in results if r["rot_score"] > 0],
                    key=lambda r: r["rot_score"], reverse=True)

    return {
        "results": results,
        "sells": sells,
        "buy_orders": buys,
        "sell_notes": sell_notes,
        "buy_notes": buy_notes,
        "ranked": ranked,
        "satellite_mv": satellite_mv,
        "held_codes": held_codes,
        "locked": check_pe_lock(pe_pct) or hard_intercept,
    }
