# -*- coding: utf-8 -*-
"""
===================================
市场环境开仓门控模块
===================================

职责：
1. _check_hard_intercept(): 硬拦截层，检查极端环境
2. check_market_gate(): 4 项门控 + 硬拦截，判断是否允许开仓
3. _detect_regime(): 根据均线状态判断市场结构（5 级）

门控条件（≥2 项通过才开仓）：
① 上证收盘 > MA20
②a 两市成交额 ≥ 1.5 万亿（DataFetcherManager 全市场统计）
②b 成交量 > 近 20 日均量（上证指数日线成交量同比）
③ 涨停 ≥ 30 且涨停 > 跌停 × 1.5

硬拦截（触发任一即锁仓）：
- 成交额冰点：两市成交额 < 1.5 万亿 且连续 ≥ 3 天（同②a数据源）
- 千股跌停：跌停 ≥ 50 且跌停 > 涨停 × 3
- 指数暴跌：上证单日跌幅 > 3%
- 成交量骤降：当日成交量 < 近 20 日均量 × 0.5
"""

import logging
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ── 简单持久化：硬拦截中"成交额冰点连续天数"的记录文件 ──
import os
import json

def _ice_days_path():
    from pathlib import Path
    return Path(__file__).parent.parent.parent / "data" / "market_gate_ice_days.json"


def _load_ice_days() -> int:
    try:
        path = _ice_days_path()
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            return int(data.get("ice_days", 0))
    except Exception:
        pass
    return 0


def _save_ice_days(days: int):
    try:
        path = _ice_days_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ice_days": days, "updated": date.today().isoformat()}), encoding='utf-8')
    except Exception:
        pass


def _detect_regime(index_df, met_count: int) -> str:
    """根据指数均线状态判断市场结构

    判定优先级：trending_down > trending_up > sideways > weak_up > chaos

    Returns:
        trending_up   — 均线多头排列 + 收盘在 MA10 上方 + 门控通过
        trending_down — 均线空头排列 + 收盘在 MA10 下方
        sideways      — 收盘紧贴 MA20（偏离 < 1.5%）
        weak_up       — 收盘在 MA20 上方 + 门控通过，但非明确多头
        chaos         — 数据不足或无法判断
    """
    try:
        if index_df is None or len(index_df) < 20:
            return "chaos"
        ma5 = index_df['close'].rolling(5).mean().iloc[-1]
        ma10 = index_df['close'].rolling(10).mean().iloc[-1]
        ma20 = index_df['close'].rolling(20).mean().iloc[-1]
        close = index_df['close'].iloc[-1]
        if any(pd.isna(x) for x in [ma5, ma10, ma20]):
            return "chaos"

        # ① trending_down — 均线空头，最高优先级
        if ma5 < ma10 < ma20 and close < ma10:
            return "trending_down"

        # ② trending_up — 均线多头 + 门控通过
        if ma5 > ma10 > ma20 and close > ma10 and met_count >= 2:
            return "trending_up"

        # ③ sideways — 紧贴 MA20 震荡
        if abs(close - ma20) / ma20 < 0.015:
            return "sideways"

        # ④ weak_up — 在 MA20 上方 + 门控通过，但不是标准多头排列
        if close > ma20 and met_count >= 2:
            return "weak_up"

        # ⑤ chaos — 其余情况
        return "chaos"
    except Exception:
        pass
    return "chaos"


def _check_hard_intercept(
    index_df,
    total_amount_yi: float,
    limit_up: int,
    limit_down: int,
    details: List[str],
) -> Tuple[bool, str]:
    """检查硬拦截条件

    Returns:
        (is_intercepted, reason)
    """
    if index_df is None or len(index_df) < 20:
        return False, ""

    close = index_df['close'].iloc[-1]
    prev_close = index_df['close'].iloc[-2] if len(index_df) >= 2 else close
    idx_pct = (close - prev_close) / prev_close * 100 if prev_close else 0

    # 拦截 1：成交量骤降（当日 < 近20日均量 × 0.5）
    if 'volume' in index_df.columns:
        latest_vol = index_df['volume'].iloc[-1]
        avg_vol = index_df['volume'].iloc[-20:].mean()
        if pd.notna(avg_vol) and avg_vol > 0 and latest_vol < avg_vol * 0.5:
            return True, f"🔴 硬拦截-成交量骤降：当日沪市成交量{latest_vol/1e8:.1f}亿股 < 20日均量{avg_vol/1e8:.1f}亿股×0.5"

    # 拦截 2：成交额冰点（两市 < 1.5 万亿 且连续 ≥ 3 天）
    ice_days = _load_ice_days()
    if total_amount_yi > 0 and total_amount_yi < 15000:
        ice_days += 1
        _save_ice_days(ice_days)
        if ice_days >= 3:
            return True, f"🔴 硬拦截-成交额冰点：两市成交额{total_amount_yi:.0f}亿 < 1.5万亿，已连续{ice_days}天"
    else:
        if ice_days > 0:
            _save_ice_days(0)

    # 拦截 3：指数暴跌（上证单日跌幅 > 3%）
    if idx_pct < -3:
        return True, f"🔴 硬拦截-指数暴跌：上证单日跌幅{idx_pct:.1f}%"

    # 拦截 4：千股跌停（跌停 ≥ 50 且跌停 > 涨停 × 3）
    if limit_down >= 50 and limit_down > limit_up * 3:
        return True, f"🔴 硬拦截-千股跌停：跌停{limit_down}家 ≥ 50 且 > 涨停{limit_up}家×3"

    return False, ""


def check_market_gate() -> Tuple[bool, Dict[str, bool], str, str, bool]:
    """
    市场环境门控 + 硬拦截

    先执行硬拦截，再执行 4 项门控检查。

    Returns:
        can_trade           — 是否允许开仓
        conditions_dict     — 各门控项通过情况
        summary_str         — 人类可读的检查摘要
        regime              — 市场状态：trending_up | trending_down | sideways | weak_up | chaos
        hard_intercept      — 是否触发了硬拦截
    """
    conditions: Dict[str, bool] = {
        "上证收盘>MA20": False,
        "两市成交额≥1.5万亿": False,
        "成交量>近20日均量": False,
        "涨停≥30且>跌停×1.5": False,
    }
    met_count = 0
    details: List[str] = []
    index_df = None
    total_amount_yi = 0.0
    limit_up = 0
    limit_down = 0

    try:
        import akshare as ak

        # ── 获取上证指数日线数据 ──
        index_df = ak.stock_zh_index_daily(symbol="sh000001")
        if index_df is not None and len(index_df) >= 20:
            index_df = index_df.sort_values('date').reset_index(drop=True)
            index_df['ma20'] = index_df['close'].rolling(window=20).mean()
            latest = index_df.iloc[-1]
            idx_close = latest['close']
            idx_ma20 = latest['ma20']

            # ① 上证 > MA20
            if pd.notna(idx_ma20) and idx_close > idx_ma20:
                conditions["上证收盘>MA20"] = True
                met_count += 1
                details.append(f"✅ 上证{idx_close:.0f} > MA20{idx_ma20:.0f}")
            else:
                details.append(f"❌ 上证{idx_close:.0f} ≤ MA20{idx_ma20:.0f}")

            # ②a 两市成交额 ≥ 1.5 万亿（DataFetcherManager 全市场统计）
            try:
                from data_provider.base import DataFetcherManager
                fm = DataFetcherManager()
                market_stats = fm.get_market_stats()
                if market_stats:
                    total_amount_yi = market_stats.get('total_amount', 0)
            except Exception:
                total_amount_yi = 0.0

            if total_amount_yi >= 15000:
                conditions["两市成交额≥1.5万亿"] = True
                met_count += 1
                details.append(f"✅ 两市成交额{total_amount_yi:.0f}亿 ≥ 1.5万亿")
            elif total_amount_yi > 0:
                details.append(f"❌ 两市成交额{total_amount_yi:.0f}亿 < 1.5万亿")
            else:
                details.append("⚠️ 获取两市成交额失败，跳过此项")

            # ②b 成交量 > 近20日均量（用成交量同比比较，单位一致无需转换）
            if 'volume' in index_df.columns:
                latest_vol = latest.get('volume', 0)
                avg_vol = index_df['volume'].iloc[-20:].mean()
                if pd.notna(avg_vol) and avg_vol > 0 and latest_vol > avg_vol:
                    conditions["成交量>近20日均量"] = True
                    met_count += 1
                    details.append(f"✅ 沪市成交量{latest_vol/1e8:.1f}亿股 > 20日均量{avg_vol/1e8:.1f}亿股")
                else:
                    details.append(f"❌ 沪市成交量{latest_vol/1e8:.1f}亿股 ≤ 20日均量{avg_vol/1e8:.1f}亿股")

        # ── ③ 涨停 ≥ 30 且涨停 > 跌停 × 1.5 ──
        limit_up = 0
        limit_down = 0
        try:
            today_str = date.today().strftime("%Y%m%d")
            zt_df = ak.stock_zt_pool_em(date=today_str)
            if zt_df is not None and not zt_df.empty:
                limit_up = len(zt_df)
                dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
                limit_down = len(dt_df) if dt_df is not None else 0

                up_ok = limit_up >= 30
                ratio_ok = limit_up > limit_down * 1.5
                if up_ok and ratio_ok:
                    conditions["涨停≥30且>跌停×1.5"] = True
                    met_count += 1
                    details.append(f"✅ 涨停{limit_up}家(≥30) > 跌停{limit_down}家")
                elif not up_ok:
                    details.append(f"❌ 涨停{limit_up}家 < 30（数量不足）")
                else:
                    details.append(f"❌ 涨停{limit_up}家 ≤ 跌停{limit_down}家×1.5（比值不足）")
        except Exception:
            details.append("⚠️ 获取涨跌停数据失败，跳过此项")

    except Exception as e:
        logger.error(f"市场门控检查失败: {e}")
        return True, conditions, "市场环境检查失败（默认放行）", "chaos", False

    # ── 硬拦截检查 ──
    is_hard, hard_reason = _check_hard_intercept(
        index_df, total_amount_yi, limit_up, limit_down, details
    )
    if is_hard:
        logger.warning(hard_reason)
        summary = f"🔴 **硬拦截触发**\n{hard_reason}\n\n" + "\n".join(details)
        return False, conditions, summary, "chaos", True

    # ── 门控判定 ──
    # 先判定市场状态（均线结构），再结合门控项数决定是否开仓
    regime = _detect_regime(index_df if index_df is not None else None, met_count)

    if regime == "trending_down":
        # 均线空头时无论门控通过多少项都不开仓：
        # 空头结构下"高成交+高情绪"组合是下跌中继/放量出货的典型特征，不是反转信号。
        # 趋势策略坚持"底部偏右进场"，等收盘重回 MA20（weak_up/trending_up）再参与。
        can_trade = False
        details.append("📉 均线空头排列，无论门控通过多少项均禁止开仓（等收盘重回MA20）")
    else:
        can_trade = met_count >= 2

    env_icon = "✅ 允许开仓" if can_trade else "❌ 建议空仓"
    summary = (
        f"市场环境检查：满足{met_count}/4项条件 → {env_icon}\n"
        + "\n".join(details)
    )

    if can_trade:
        logger.info(f"✅ 市场环境满足开仓条件（{met_count}/4，状态: {regime}）")
    else:
        logger.warning(f"⛔ 市场环境不满足开仓条件（仅{met_count}/4，状态: {regime}）")

    return can_trade, conditions, summary, regime, False
