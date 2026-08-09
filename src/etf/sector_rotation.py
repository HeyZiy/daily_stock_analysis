# -*- coding: utf-8 -*-
"""
===================================
板块轮动引擎
===================================

按行业动量排名从卫星仓池中选择标的，独立于核心仓再平衡。

得分维度：20日涨跌幅 + 5日涨跌幅 + 均线排列 + 量能确认
进入规则：连续 2 天排名前 3 → 确认进入
退出规则：得分 < 40 或连续 3 天不在前 3
仓位上限：总资产 10%，等权分配
仅在 PE 分位 < 60%（非高估）时激活
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent.parent / "data" / "rotation_state.json"

MAX_BUDGET_RATIO = 0.10   # 卫星仓总仓位上限
TOP_N = 2                  # 选前 N 名
ENTRY_DAYS = 2             # 连续排名前 3 天数要求
EXIT_DAYS = 3              # 连续不在前 3 天数触发退出
MIN_SCORE = 40             # 得分低于此值直接退出
PE_THRESHOLD = 60          # PE 分位超过此值禁止持仓


@dataclass
class RotationOrder:
    code: str
    name: str
    action: str      # buy / sell
    shares: int
    price: float
    amount: float
    reason: str


# ── 状态持久化 ──

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"top3_streak": {}, "not_top3_streak": {}, "active": {}}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 数据获取 + 评分 ──

def _fetch_etf_daily(code: str) -> Optional[pd.DataFrame]:
    """获取 ETF 日线数据"""
    try:
        import akshare as ak
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            return df
    except Exception:
        pass

    try:
        from data_provider.base import DataFetcherManager
        fm = DataFetcherManager()
        df = fm.get_daily_kline(code, days=60)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass

    return None


def _score_etf(code: str, name: str) -> dict:
    """对单个 ETF 打分（0-100）"""
    df = _fetch_etf_daily(code)
    if df is None or len(df) < 25:
        return {"code": code, "name": name, "score": 0, "error": True}

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    latest = df.iloc[-1]

    # 20日涨跌幅（0-30分）
    ret_20d = (df["close"].iloc[-1] / df["close"].iloc[-20] - 1) * 100 if len(df) >= 20 else 0
    if ret_20d > 10:
        score_20d = 30
    elif ret_20d > 5:
        score_20d = 20 + (ret_20d - 5) * 2
    elif ret_20d > 0:
        score_20d = 5 + ret_20d * 3
    elif ret_20d > -5:
        score_20d = max(0, 5 + ret_20d)
    else:
        score_20d = 0

    # 5日涨跌幅（0-25分）
    ret_5d = (df["close"].iloc[-1] / df["close"].iloc[-5] - 1) * 100 if len(df) >= 5 else 0
    if ret_5d > 5:
        score_5d = 25
    elif ret_5d > 2:
        score_5d = 15 + (ret_5d - 2) * 3.3
    elif ret_5d > 0:
        score_5d = 5 + ret_5d * 5
    elif ret_5d > -3:
        score_5d = max(0, 5 + ret_5d * 1.6)
    else:
        score_5d = 0

    # 均线排列（0-25分）
    ma5, ma10, ma20 = latest.get("ma5"), latest.get("ma10"), latest.get("ma20")
    if pd.notna(ma5) and pd.notna(ma10) and pd.notna(ma20):
        if ma5 > ma10 > ma20:
            score_ma = 25
        elif ma5 > ma10:
            score_ma = 12
        elif df["close"].iloc[-1] > ma20:
            score_ma = 5
        else:
            score_ma = 0
    else:
        score_ma = 0

    # 量能确认（0-20分）
    vol_ma5 = latest.get("vol_ma5")
    vol_ma20 = latest.get("vol_ma20")
    if pd.notna(vol_ma5) and pd.notna(vol_ma20) and vol_ma20 > 0:
        ratio = vol_ma5 / vol_ma20
        if ratio > 1.5:
            score_vol = 20
        elif ratio > 1.0:
            score_vol = 10 + (ratio - 1.0) * 20
        elif ratio > 0.8:
            score_vol = 5 + (ratio - 0.8) * 25
        else:
            score_vol = 0
    else:
        score_vol = 0

    # 行业估值/拥挤度因子（AmazingData，失败不影响打分）
    industry = None
    pe_pct = pb_pct = share_pct = None
    score_adj = 0
    try:
        from src.etf.amazing_factors import (
            get_etf_industry,
            get_industry_code_by_name,
            get_industry_pe_percentile,
            get_industry_mcap_share,
        )

        industry = get_etf_industry(code)
        if industry:
            ind_code = get_industry_code_by_name(industry)
            if ind_code:
                pe_info = get_industry_pe_percentile(ind_code)
                if pe_info:
                    pe_pct = pe_info["pe_pct"]
                    if pe_pct < 30:
                        score_adj += 10      # 行业低估，加分
                    elif pe_pct < 60:
                        score_adj += 5       # 合理
                    elif pe_pct >= 80:
                        score_adj -= 10      # 行业高估，回避
                mcap_info = get_industry_mcap_share(ind_code)
                if mcap_info:
                    share_pct = mcap_info["share_pct"]
                    if share_pct > 90:
                        score_adj -= 5       # 拥挤度警示
    except Exception as e:
        logger.debug(f"行业因子获取失败（{code}）: {e}")

    total = round(score_20d + score_5d + score_ma + score_vol + score_adj)
    return {
        "code": code, "name": name, "score": min(95, max(0, total)),
        "ret_20d": round(ret_20d, 1), "ret_5d": round(ret_5d, 1),
        "ma_align": score_ma, "vol_ok": score_vol,
        "industry": industry, "pe_pct": pe_pct, "pb_pct": pb_pct,
        "share_pct": share_pct, "score_adj": score_adj,
    }


def score_all_pool(pool) -> List[dict]:
    """对卫星仓池中所有 ETF 打分并排名"""
    results = []
    for asset in pool:
        res = _score_etf(asset.code, asset.name)
        results.append(res)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ── 决策逻辑 ──

def _decide_rotation(scores: List[dict], held_codes: set, pe_pct: float) -> Tuple[List[str], List[str], str]:
    """决定哪些 ETF 进入、哪些退出

    Returns:
        enter_codes: 应该买入的代码列表
        exit_codes: 应该卖出的代码列表
        summary: 可读摘要
    """
    state = _load_state()

    # PE 高估 → 清仓所有卫星
    if pe_pct >= PE_THRESHOLD:
        logger.info(f"PE 分位 {pe_pct:.0f}% >= {PE_THRESHOLD}%，清仓所有卫星仓")
        exit_all = list(held_codes)
        state["active"] = {}
        state["top3_streak"] = {}
        _save_state(state)
        return [], exit_all, f"PE 分位 {pe_pct:.0f}% 偏高估，卫星仓全部清退"

    ranked = [s["code"] for s in scores if s["score"] > 0 and not s.get("error")]
    top3 = ranked[:3]

    # 更新排名连续天数
    for code in ranked:
        if code in top3:
            state["top3_streak"][code] = state["top3_streak"].get(code, 0) + 1
            state["not_top3_streak"][code] = 0
        else:
            state["not_top3_streak"][code] = state["not_top3_streak"].get(code, 0) + 1
            state["top3_streak"][code] = 0

    # 决定进入
    enter_codes = []
    for code in top3[:TOP_N]:
        if code in held_codes:
            continue  # 已持有
        if state["top3_streak"].get(code, 0) >= ENTRY_DAYS:
            enter_codes.append(code)

    # 决定退出
    exit_codes = []
    for code in list(held_codes):
        score = next((s["score"] for s in scores if s["code"] == code), 0)
        # 条件1：得分跌破阈值
        if score < MIN_SCORE:
            exit_codes.append(code)
            continue
        # 条件2：连续不在前3
        if state["not_top3_streak"].get(code, 0) >= EXIT_DAYS:
            exit_codes.append(code)
            continue

    # 如果买入触发，同时卖出排名靠后的已有持仓腾仓位
    if enter_codes:
        # 保持最多 TOP_N 个持仓
        all_entered = [c for c in held_codes if c not in exit_codes] + enter_codes
        while len(all_entered) > TOP_N:
            # 踢掉排名最低的
            for c in reversed(ranked):
                if c in all_entered and c not in enter_codes:
                    exit_codes.append(c)
                    all_entered.remove(c)
                    break
            else:
                break

    # 更新 active 状态
    new_active = {c: True for c in (set(held_codes) - set(exit_codes)) | set(enter_codes)}
    state["active"] = new_active
    _save_state(state)

    # 生成摘要
    lines = []
    for s in scores[:6]:
        rank_mark = ""
        if s["code"] in enter_codes:
            rank_mark = " ⬆️ 触发买入"
        elif s["code"] in exit_codes:
            rank_mark = " ⬇️ 触发卖出"
        elif s["code"] in held_codes:
            rank_mark = " ✓ 持有中"
        elif s["code"] in top3:
            streak = state["top3_streak"].get(s["code"], 0)
            rank_mark = f" 候选({streak}/{ENTRY_DAYS})"
        lines.append(
            f"  {s['name']}({s['code']}) 得分:{s['score']:2d}  "
            f"20日:{s.get('ret_20d',0):+.1f}% 5日:{s.get('ret_5d',0):+.1f}%{rank_mark}"
        )

    summary = "\n".join(lines)
    return enter_codes, exit_codes, summary


# ── 主入口 ──

def run_rotation(pool, positions: List[dict], total_assets: float, pe_pct: float) -> Tuple[List[RotationOrder], str]:
    """板块轮动主入口

    Args:
        pool: SATELLITE_POOL 列表
        positions: 当前所有持仓（含卫星 ETF）
        total_assets: 总资产
        pe_pct: PE 分位

    Returns:
        orders: 调仓指令列表
        summary: 报告用摘要
    """
    # 识别当前卫星持仓
    pool_codes = {a.code for a in pool}
    held = {}
    for p in positions:
        if p["code"] in pool_codes and p.get("count", 0) > 0:
            held[p["code"]] = {
                "shares": p["count"],
                "price": p.get("current_price", 0),
                "name": p.get("name", ""),
            }

    # 打分
    scores = score_all_pool(pool)

    # 决策
    enter_codes, exit_codes, rank_summary = _decide_rotation(
        scores, set(held.keys()), pe_pct
    )

    # 生成订单
    orders: List[RotationOrder] = []
    satellite_budget = total_assets * MAX_BUDGET_RATIO
    per_position = satellite_budget / max(TOP_N, 1)

    score_map = {s["code"]: s for s in scores}

    # 卖出
    for code in exit_codes:
        if code in held:
            h = held[code]
            price = h["price"] if h["price"] > 0 else _get_price(code)
            orders.append(RotationOrder(
                code=code, name=h["name"], action="sell",
                shares=h["shares"], price=price,
                amount=h["shares"] * price,
                reason="得分跌破阈值" if score_map.get(code, {}).get("score", 0) < MIN_SCORE else "排名下滑退出",
            ))

    # 买入
    for code in enter_codes:
        price = _get_price(code)
        if price <= 0:
            continue
        target_shares = max(100, int(per_position / price / 100) * 100)
        orders.append(RotationOrder(
            code=code, name=score_map.get(code, {}).get("name", ""), action="buy",
            shares=target_shares, price=price,
            amount=target_shares * price,
            reason=f"连续{ENTRY_DAYS}天排名前3，确认进入",
        ))

    # 汇总
    header = f"## 卫星仓 — 板块轮动\n\nPE分位: {pe_pct:.0f}%"
    if pe_pct >= PE_THRESHOLD:
        header += f" — 🔒 高估锁仓，卫星仓不激活\n\n"
    else:
        header += f" | 仓位上限: {MAX_BUDGET_RATIO*100:.0f}% | 最多 {TOP_N} 只\n\n"

    header += "### 当前排名\n\n"
    header += rank_summary

    if orders:
        header += "\n\n### 轮动调仓\n\n"
        for o in orders:
            header += f"- {'🟢买入' if o.action == 'buy' else '🔴卖出'} {o.name}({o.code}) {o.shares}股 ≈ {o.amount:,.0f}元 — {o.reason}\n"

    if not orders and not enter_codes and not exit_codes:
        if pe_pct < PE_THRESHOLD:
            held_list = [f"{h['name']}({c})" for c, h in held.items()]
            if held_list:
                header += f"\n无调仓 | 当前持有: {', '.join(held_list)}\n"
            else:
                header += "\n无调仓 | 当前无卫星持仓\n"

    return orders, header


def _get_price(code: str) -> float:
    """获取 ETF 当前价格"""
    try:
        import akshare as ak
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
        if df is not None and not df.empty:
            return float(df.sort_values("date").iloc[-1]["close"])
    except Exception:
        pass
    return 0.0
