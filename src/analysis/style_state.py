# -*- coding: utf-8 -*-
"""
===================================
风格状态判定 — Style State
===================================

周度风格状态播报器（纯规则，无 LLM）：

1. 拉取宽基指数 + 申万一级行业日线（akshare 免费源，全历史）
2. 计算风格指标：主线组动量领先度 / 领涨持续性 / 大小盘 / 风格簇收益 / 市场宽度
3. 状态机判定（带滞回）：
   - strong  主线强势期：领先 spread 够大、领涨组稳定且仍在涨，连续 2 周确认
   - fading  退潮期：原领涨组转跌或 spread 崩塌（仅从强势期进入）
   - vacuum  真空期：领先 spread 不足或领涨组每周大洗牌（快速轮动）
   - forming 形成中：有苗头但未确认
4. 落盘 data/style_state.json（跨周记忆 + 历史留存，支撑"持续第 N 周"）
5. build_report() 输出周报 markdown

阈值与簇映射全部为模块顶部常量；用 style_report.py --backtest 回测调优。
外部依赖一律"尽力而为"：单行业/单指数失败跳过，估值佐证失败降级。
"""

import json
import logging
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent.parent / "data" / "style_state.json"

# ── 宽基指数（akshare 新浪指数日线 symbol）──
INDEX_MAP = {
    "上证指数": "sh000001",
    "沪深300": "sh000300",
    "上证50": "sh000016",
    "中证500": "sh000905",
    "中证1000": "sh000852",
    "创业板指": "sz399006",
    "科创50": "sh000688",
}

# ── 申万一级行业 → 风格簇（31 个全覆盖，簇映射可按需调整）──
STYLE_CLUSTERS = {
    "周期资源": ["有色金属", "基础化工", "钢铁", "建筑材料", "煤炭", "石油石化"],
    "价值防御": ["银行", "非银金融", "交通运输", "公用事业", "建筑装饰", "房地产", "环保"],
    "成长科技": ["电子", "计算机", "传媒", "通信", "电力设备", "国防军工", "机械设备", "汽车"],
    "消费医药": ["农林牧渔", "家用电器", "食品饮料", "纺织服饰", "轻工制造", "医药生物", "商贸零售", "社会服务", "美容护理"],
    "综合": ["综合"],
}
_INDUSTRY_CLUSTER = {ind: c for c, inds in STYLE_CLUSTERS.items() for ind in inds}

# ── 动量窗口（交易日）──
MOM_SHORT = 20    # ≈1 个月，主线判定主信号
MOM_LONG = 60     # ≈1 季度，趋势佐证
WEEK_DAYS = 5

# ── 状态判定阈值（v0 默认值，待回测调优）──
SPREAD_STRONG = 5.0        # 领先 spread ≥ 此值才可能有主线（Top5 20d 均值 − 全行业中位数）
SPREAD_KEEP = 2.0          # 强势期维持下限，跌破转退潮
SPREAD_VACUUM = 3.0        # spread ≤ 此值视为无主线
SPREAD_VACUUM_EXIT = 4.5   # 真空期退出死区上沿：spread 需回到此值上方才脱离真空（防止 3% 附近周周翻转）
RETENTION_STRONG = 0.6     # 领涨组周度重合率 ≥ 3/5 才算持续主线
RETENTION_VACUUM = 0.4     # 重合率 ≤ 2/5 视为领涨组快速更替
LEADER_FADE = -2.0         # 上周 Top5 本周 5d 均值 ≤ 此值 → 领涨组退潮
STRONG_CONFIRM_WEEKS = 2   # 强势条件连续 N 周满足才转入强势期
VACUUM_CONFIRM_WEEKS = 2   # 真空条件连续 N 周满足才转入真空期（单周领涨组洗牌只算形成中）
SIZE_THRESHOLD = 1.5       # 大小盘 20d 相对收益超过此值才标注占优方向

# ── 状态常量 ──
S_STRONG, S_FADING, S_VACUUM, S_FORMING = "strong", "fading", "vacuum", "forming"
STATE_LABELS = {
    S_STRONG: "🟢 主线强势期",
    S_FADING: "🔴 退潮期",
    S_VACUUM: "⚪ 风格真空期",
    S_FORMING: "🟡 形成中",
}


# ══════════════ 数据获取 ══════════════

def fetch_index_daily() -> Dict[str, pd.DataFrame]:
    """宽基指数日线（akshare 新浪源，全历史）。失败的跳过。

    Returns: {指数名: df(date, close)}，date 为 datetime 升序
    """
    import akshare as ak

    out = {}
    for name, sym in INDEX_MAP.items():
        try:
            df = ak.stock_zh_index_daily(symbol=sym)
            if df is not None and len(df) > MOM_LONG + 10:
                df = df.copy()
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
                out[name] = df[["date", "close"]]
        except Exception as e:
            logger.warning(f"获取指数 {name} 失败: {e}")
    logger.info(f"指数日线获取完成: {len(out)}/{len(INDEX_MAP)}")
    return out


def fetch_industry_daily() -> Dict[str, pd.DataFrame]:
    """申万一级行业日线（akshare index_hist_sw，约 2014 至今）。

    Returns: {行业名: df(date, close, amount)}，date 为 datetime 升序
    """
    import akshare as ak

    out = {}
    try:
        info = ak.sw_index_first_info()
    except Exception as e:
        logger.error(f"获取申万行业清单失败: {e}")
        return out

    for _, row in info.iterrows():
        code = str(row["行业代码"]).replace(".SI", "")
        name = str(row["行业名称"])
        try:
            df = ak.index_hist_sw(symbol=code, period="day")
            if df is not None and len(df) > MOM_LONG + 10:
                df = df.rename(columns={"日期": "date", "收盘": "close", "成交额": "amount"})
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
                out[name] = df[["date", "close", "amount"]]
        except Exception as e:
            logger.warning(f"获取行业 {name} 日线失败: {e}")
        time.sleep(0.3)  # 逐行业请求，限速保护
    logger.info(f"行业日线获取完成: {len(out)}/{len(info)}")
    return out


def _slice_as_of(df: pd.DataFrame, as_of: Optional[date]) -> pd.DataFrame:
    """截取 as_of（含）之前的数据；as_of 为 None 取全部。"""
    if as_of is None:
        return df
    return df[df["date"] <= pd.Timestamp(as_of)]


def _trailing_ret(close: pd.Series, days: int) -> Optional[float]:
    """close 序列（升序）最近 days 个交易日的涨跌幅（%）。数据不足返回 None。"""
    if len(close) < days + 1:
        return None
    return (float(close.iloc[-1]) / float(close.iloc[-days - 1]) - 1) * 100


def _retention_txt(retention: Optional[float]) -> str:
    """领涨组重合率的展示文本（无上周记录时标注）。"""
    return "无记录" if retention is None else f"{retention:.0%}"


# ══════════════ 指标计算 ══════════════

def compute_snapshot(
    industries: Dict[str, pd.DataFrame],
    indices: Optional[Dict[str, pd.DataFrame]] = None,
    prev_top5: Optional[List[str]] = None,
    as_of: Optional[date] = None,
) -> Optional[dict]:
    """截至 as_of（含）计算风格指标快照。有效行业不足 20 个返回 None。"""
    rets20, rets5, rets60, above = {}, {}, {}, {}
    data_date = None
    for name, df in industries.items():
        d = _slice_as_of(df, as_of)
        if len(d) < MOM_LONG + 1:
            continue
        close = d["close"].astype(float)
        rets20[name] = _trailing_ret(close, MOM_SHORT)
        rets5[name] = _trailing_ret(close, WEEK_DAYS)
        rets60[name] = _trailing_ret(close, MOM_LONG)
        above[name] = float(close.iloc[-1]) > float(close.tail(MOM_SHORT).mean())
        latest = d["date"].iloc[-1]
        if data_date is None or latest > data_date:
            data_date = latest
    if len(rets20) < 20:
        return None

    # 主线组（20d 动量 Top5）
    top5 = sorted(rets20, key=rets20.get, reverse=True)[:5]
    bottom5 = sorted(rets20, key=rets20.get)[:5]
    median20 = float(np.median(list(rets20.values())))
    spread = float(np.mean([rets20[n] for n in top5]) - median20)

    # 领涨持续性：与上周 Top5 的重合率；上周领涨组本周表现
    prev_top5 = [n for n in (prev_top5 or []) if n in rets5]
    if prev_top5:
        retention = len(set(top5) & set(prev_top5)) / 5.0
        prev_leaders_ret5 = float(np.mean([rets5[n] for n in prev_top5]))
        leaders_fading = prev_leaders_ret5 <= LEADER_FADE
    else:
        retention = None            # 无上周记录，不参与判定
        prev_leaders_ret5 = None
        leaders_fading = False

    # 风格簇收益（等权）
    cluster_rets = {}
    for cluster, members in STYLE_CLUSTERS.items():
        m20 = [rets20[m] for m in members if m in rets20]
        m5 = [rets5[m] for m in members if m in rets5]
        if m20:
            cluster_rets[cluster] = {
                "ret20": float(np.mean(m20)),
                "ret5": float(np.mean(m5)) if m5 else None,
            }

    # 大小盘（中证1000 − 沪深300）
    size20 = size60 = None
    if indices and "中证1000" in indices and "沪深300" in indices:
        c1000 = _slice_as_of(indices["中证1000"], as_of)["close"].astype(float)
        c300 = _slice_as_of(indices["沪深300"], as_of)["close"].astype(float)

        def _spread(days: int) -> Optional[float]:
            a, b = _trailing_ret(c1000, days), _trailing_ret(c300, days)
            return a - b if a is not None and b is not None else None

        size20 = _spread(MOM_SHORT)
        size60 = _spread(MOM_LONG)

    return {
        "data_date": str(data_date.date()) if data_date is not None else None,
        "top5": top5,
        "bottom5": bottom5,
        "top5_detail": [
            {
                "name": n,
                "ret20": round(rets20[n], 2),
                "ret5": round(rets5[n], 2),
                "ret60": round(rets60[n], 2),
                "above_ma20": above[n],
                "cluster": _INDUSTRY_CLUSTER.get(n, "未知"),
            }
            for n in top5
        ],
        "bottom5_detail": [
            {"name": n, "ret20": round(rets20[n], 2), "cluster": _INDUSTRY_CLUSTER.get(n, "未知")}
            for n in bottom5
        ],
        "median_ret20": round(median20, 2),
        "spread": round(spread, 2),
        "retention": None if retention is None else round(retention, 2),
        "top5_ret5": round(float(np.mean([rets5[n] for n in top5])), 2),
        "prev_leaders_ret5": None if prev_leaders_ret5 is None else round(prev_leaders_ret5, 2),
        "leaders_fading": leaders_fading,
        "cluster_rets": cluster_rets,
        "breadth": round(sum(above.values()) / len(above), 3),
        "size20": None if size20 is None else round(size20, 2),
        "size60": None if size60 is None else round(size60, 2),
    }


def dominant_style(snap: dict) -> str:
    """主导风格标签：Top5 中占多数的簇 + 大小盘方向。"""
    counts = Counter(t["cluster"] for t in snap["top5_detail"])
    main = counts.most_common(1)[0][0] if counts else "未知"

    size_tag = ""
    if snap.get("size20") is not None:
        if snap["size20"] >= SIZE_THRESHOLD:
            size_tag = " · 小盘占优"
        elif snap["size20"] <= -SIZE_THRESHOLD:
            size_tag = " · 大盘占优"
    return f"{main}{size_tag}"


# ══════════════ 状态机 ══════════════

def classify(snap: dict, prev: dict) -> Tuple[str, dict, List[str]]:
    """
    状态机判定（带滞回 + 双向确认）。

    Args:
        snap: compute_snapshot 输出
        prev: 上周状态 {"state", "strong_cand_streak", "vacuum_cand_streak", "prev_top5"}
    Returns:
        (state, 候选计数 {"strong_cand": n, "vacuum_cand": n}, 判定依据列表)
    """
    spread = snap["spread"]
    retention = snap["retention"]
    top5_ret5 = snap["top5_ret5"]
    prev_state = prev.get("state")

    strong_cond = (
        spread >= SPREAD_STRONG
        and retention is not None
        and retention >= RETENTION_STRONG
        and top5_ret5 > 0
    )
    vacuum_cond = (
        spread <= SPREAD_VACUUM
        or (retention is not None and retention <= RETENTION_VACUUM)
    )
    streaks = {
        "strong_cand": (prev.get("strong_cand_streak") or 0) + 1 if strong_cond else 0,
        "vacuum_cand": (prev.get("vacuum_cand_streak") or 0) + 1 if vacuum_cond else 0,
    }

    # 1) 强势期：先查崩塌（立即退潮），再查滞回维持
    if prev_state == S_STRONG:
        if snap["leaders_fading"] or spread <= SPREAD_KEEP:
            reason = (
                f"上周领涨组本周 5d 均值 {snap['prev_leaders_ret5']:+.1f}%（≤{LEADER_FADE}%）"
                if snap["leaders_fading"] else f"领先 spread 收窄至 {spread:.1f}%（≤{SPREAD_KEEP}%）"
            )
            return S_FADING, streaks, [reason]
        if retention is None or retention > RETENTION_VACUUM:
            return S_STRONG, streaks, [f"强势延续（spread {spread:.1f}%，重合率 {_retention_txt(retention)}）"]
        return S_VACUUM, streaks, ["领涨组大幅换血，主线瓦解"]

    # 2) 退潮期：领涨组仍在跌则延续，企稳后进入后续判定
    if prev_state == S_FADING and snap["leaders_fading"]:
        return S_FADING, streaks, [f"前期领涨组仍在下跌（5d 均值 {snap['prev_leaders_ret5']:+.1f}%）"]

    # 3) 真空/快速轮动
    #    进入需连续 VACUUM_CONFIRM_WEEKS 周确认；退出需 spread 回到死区上沿且重合率不差
    if prev_state == S_VACUUM:
        vacuum_exit_ok = spread > SPREAD_VACUUM_EXIT and (retention is None or retention > RETENTION_VACUUM)
        if not vacuum_exit_ok:
            if spread <= SPREAD_VACUUM_EXIT:
                reason = f"真空延续（spread {spread:.1f}% 未回升至 {SPREAD_VACUUM_EXIT}% 上方）"
            else:
                reason = f"真空延续（重合率 {_retention_txt(retention)}，领涨组仍在快速轮动）"
            return S_VACUUM, streaks, [reason]
        # 满足退出条件 → 落到 4/5 重新评估
    elif vacuum_cond:
        if streaks["vacuum_cand"] >= VACUUM_CONFIRM_WEEKS:
            reason = (
                f"领先 spread 连续偏弱（本周 {spread:.1f}%），无主线"
                if spread <= SPREAD_VACUUM
                else f"领涨组快速轮动（重合率 {retention:.0%}）已持续 {streaks['vacuum_cand']} 周"
            )
            return S_VACUUM, streaks, [reason]
        return S_FORMING, streaks, [f"真空条件第 {streaks['vacuum_cand']} 周（单周领涨组洗牌，待确认）"]

    # 4) 强势确认（连续 N 周）
    if strong_cond:
        if streaks["strong_cand"] >= STRONG_CONFIRM_WEEKS:
            return S_STRONG, streaks, [f"强势条件连续 {streaks['strong_cand']} 周满足（spread {spread:.1f}%，重合率 {retention:.0%}）"]
        return S_FORMING, streaks, [f"强势条件第 {streaks['strong_cand']} 周满足，待确认"]

    # 5) 其余
    if spread >= SPREAD_STRONG and (retention is None or retention >= RETENTION_STRONG) and top5_ret5 <= 0:
        return S_FORMING, streaks, [f"spread {spread:.1f}% 够大，但领涨组近 5d 均值 {top5_ret5:+.1f}%，涨势暂歇"]
    return S_FORMING, streaks, [f"主线未确认（spread {spread:.1f}%，重合率 {_retention_txt(retention)}）"]


# ══════════════ 状态持久化 ══════════════

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "history" in data:
                return data
    except Exception as e:
        logger.warning(f"风格状态文件读取失败，重置: {e}")
    return {
        "updated": None, "data_date": None, "state": None, "state_streak": 0,
        "strong_cand_streak": 0, "vacuum_cand_streak": 0, "prev_top5": [], "history": [],
    }


def save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"风格状态文件写入失败: {e}")


def update_state(snap: dict, state: str, streaks: dict, prev: dict) -> dict:
    """构建新的落盘状态。

    streaks 为 classify 输出的候选计数 {"strong_cand": n, "vacuum_cand": n}。
    history 每条自带 state/state_streak/候选计数/prev_top5，
    同一数据周重跑时覆盖最后一条（不重复追加），streak 相对前一条计算。
    """
    history = list(prev.get("history", []))
    same_week = bool(history) and history[-1].get("date") == snap["data_date"]
    if same_week and len(history) >= 2:
        ref = history[-2]          # 同周重跑：streak 相对再上周算
    elif same_week:
        ref = None                 # 同周重跑且之前无更早记录：streak 从 1 起
    elif history:
        ref = history[-1]
    else:
        ref = None
    ref_state = ref.get("state") if ref else None
    state_streak = (ref.get("state_streak") or 0) + 1 if state == ref_state else 1

    entry = {
        "date": snap["data_date"],
        "state": state,
        "state_streak": state_streak,
        "strong_cand_streak": streaks.get("strong_cand", 0),
        "vacuum_cand_streak": streaks.get("vacuum_cand", 0),
        "prev_top5": prev.get("prev_top5", []),
        "dominant": dominant_style(snap),
        "spread": snap["spread"],
        "retention": snap["retention"],
        "top5_ret5": snap["top5_ret5"],
        "size20": snap["size20"],
        "size60": snap["size60"],
        "breadth": snap["breadth"],
        "top5": snap["top5"],
    }
    if same_week:
        history[-1] = entry
    else:
        history.append(entry)
    return {
        "updated": date.today().isoformat(),
        "data_date": snap["data_date"],
        "state": state,
        "state_streak": state_streak,
        "strong_cand_streak": streaks.get("strong_cand", 0),
        "vacuum_cand_streak": streaks.get("vacuum_cand", 0),
        "prev_top5": snap["top5"],
        "history": history[-300:],
    }


# ══════════════ 估值佐证（AmazingData 可用时附带，失败降级）══════════════

def pe_notes(top5_names: List[str]) -> Dict[str, str]:
    """领涨组 PE 分位注记；无 AmazingData 环境返回空 dict。"""
    try:
        from src.etf.amazing_factors import get_level1_industries, get_industry_pe_percentile

        code_map = {item["name"]: item["code"] for item in get_level1_industries() or []}
        out = {}
        for name in top5_names:
            code = code_map.get(name)
            if not code:
                continue
            r = get_industry_pe_percentile(code)
            if r:
                out[name] = f"PE {r['pe']:.1f}（{r['pe_pct']:.0f}%分位）"
        return out
    except Exception as e:
        logger.debug(f"行业 PE 佐证不可用（降级）: {e}")
        return {}


# ══════════════ 周报 ══════════════

def _format_pct(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:+.2f}%"


def _market_gate_section() -> str:
    """市场门控环境（复用现有 check_market_gate，失败降级）。"""
    lines = ["### 四、市场环境（门控）", ""]
    try:
        from src.analysis.market_gate import check_market_gate

        can_trade, conditions, _, regime, hard_intercept = check_market_gate()
        lines.append(f"- 市场状态: **{regime}** | 允许开仓: {'是' if can_trade else '否'} | 硬拦截: {'触发' if hard_intercept else '无'}")
        for name, ok in (conditions or {}).items():
            lines.append(f"  - {'✅' if ok else '❌'} {name}")
    except Exception as e:
        lines.append(f"- 获取失败: {e}")
    return "\n".join(lines)


def build_report(snap: dict, state: dict, strong_cand_streak: int, reasons: List[str]) -> str:
    """生成周报 markdown。state 为 update_state 输出的落盘状态 dict。"""
    state_str = state.get("state")
    label = STATE_LABELS.get(state_str, state_str)
    streak_txt = f"（持续第 {state['state_streak']} 周）" if state.get("state_streak", 1) > 1 else ""
    notes = pe_notes(snap["top5"])

    lines = []
    lines.append("# 🎯 风格状态周报")
    lines.append(f"**数据截至 {snap['data_date']}**（申万一级行业，20 个交易日动量口径）")
    lines.append("")
    lines.append("## 一、风格状态")
    lines.append("")
    lines.append(f"- 当前状态: **{label}**{streak_txt}")
    lines.append(f"- 主导风格: **{dominant_style(snap)}**")
    for r in reasons:
        lines.append(f"- 判定依据: {r}")
    if strong_cand_streak > 0:
        lines.append(f"- 强势确认进度: {strong_cand_streak}/{STRONG_CONFIRM_WEEKS} 周")
    lines.append("")

    # 二、主线明细
    lines.append("## 二、主线明细")
    lines.append("")
    lines.append("领涨组（20d 动量 Top5）：")
    lines.append("")
    lines.append("| # | 行业 | 簇 | 20d | 5d | 60d | 站上MA20 | 估值 |")
    lines.append("|---|------|----|-----|----|-----|---------|------|")
    for i, t in enumerate(snap["top5_detail"], 1):
        lines.append(
            f"| {i} | {t['name']} | {t['cluster']} | {t['ret20']:+.1f}% | {t['ret5']:+.1f}% "
            f"| {t['ret60']:+.1f}% | {'✅' if t['above_ma20'] else '❌'} | {notes.get(t['name'], '-')} |"
        )
    lines.append("")
    lines.append(f"- 领先 spread（Top5 均值 − 行业中位数，20d）: **{snap['spread']:+.1f}%**（中位数 {snap['median_ret20']:+.1f}%）")
    if snap["retention"] is not None:
        lines.append(f"- 领涨组重合率（vs 上周 Top5）: **{snap['retention']:.0%}**")
    if snap["prev_leaders_ret5"] is not None:
        lines.append(f"- 上周领涨组本周 5d 均值: **{snap['prev_leaders_ret5']:+.1f}%**")
    lines.append("")
    lines.append("垫底组（20d 动量 Bottom5）：")
    lines.append("")
    for i, b in enumerate(snap["bottom5_detail"], 1):
        lines.append(f"- 🔴 BOTTOM{i}: {b['name']}（{b['cluster']}）20d {b['ret20']:+.1f}%")
    lines.append("")

    # 三、风格指标
    lines.append("## 三、风格指标")
    lines.append("")
    lines.append(f"- 大小盘（中证1000 − 沪深300）: 20d **{_format_pct(snap['size20'])}** | 60d {_format_pct(snap['size60'])}")
    lines.append(f"- 市场宽度（行业站上 MA20 占比）: **{snap['breadth']:.0%}**")
    lines.append("")
    lines.append("风格簇收益（等权）：")
    lines.append("")
    lines.append("| 簇 | 20d | 5d |")
    lines.append("|----|-----|----|")
    for cluster, r in sorted(snap["cluster_rets"].items(), key=lambda kv: kv[1]["ret20"], reverse=True):
        lines.append(f"| {cluster} | {r['ret20']:+.1f}% | {_format_pct(r['ret5'])} |")
    lines.append("")

    # 四、市场环境
    lines.append(_market_gate_section())
    lines.append("")

    return "\n".join(lines)


# ══════════════ 入口（供 style_report.py 调用）══════════════

def run_weekly() -> Tuple[str, dict]:
    """周度主流程：取数 → 判定 → 更新状态 → 生成报告。"""
    industries = fetch_industry_daily()
    indices = fetch_index_daily()
    if len(industries) < 20:
        raise RuntimeError(f"行业日线仅获取 {len(industries)} 个，不足以判定风格状态")

    prev = load_state()
    snap = compute_snapshot(industries, indices, prev_top5=prev.get("prev_top5"))
    if snap is None:
        raise RuntimeError("风格指标计算失败（数据不足）")

    # 同一数据周重跑：以再上周的记录为判定基准，本周结果覆盖落盘最后一条
    history = prev.get("history", [])
    if prev.get("data_date") == snap["data_date"]:
        if len(history) >= 2:
            ref = history[-2]
            prev = {
                "state": ref.get("state"),
                "state_streak": ref.get("state_streak", 0),
                "strong_cand_streak": ref.get("strong_cand_streak", 0),
                "vacuum_cand_streak": ref.get("vacuum_cand_streak", 0),
                "prev_top5": ref.get("prev_top5", []),
                "data_date": ref.get("date"),
                "history": history,
            }
        else:
            # 落盘里只有本周一条（首次运行中断重跑）：视为全新状态
            prev = {"state": None, "state_streak": 0, "strong_cand_streak": 0,
                    "vacuum_cand_streak": 0, "prev_top5": [], "data_date": None, "history": history}
        snap = compute_snapshot(industries, indices, prev_top5=prev["prev_top5"])
        if snap is None:
            raise RuntimeError("风格指标计算失败（数据不足）")

    state, streaks, reasons = classify(snap, prev)
    new_state = update_state(snap, state, streaks, prev)
    save_state(new_state)
    report = build_report(snap, new_state, streaks.get("strong_cand", 0), reasons)
    return report, new_state


def run_backtest(start: str) -> str:
    """历史回测：从 start 起逐周末重放判定（无未来函数），输出状态时间线。

    不写状态文件、不发通知。
    """
    industries = fetch_industry_daily()
    indices = fetch_index_daily()
    if len(industries) < 20:
        raise RuntimeError(f"行业日线仅获取 {len(industries)} 个，不足以回测")

    # 以银行行业日历为基准，取每周最后一个交易日作为评估点
    ref = industries.get("银行")
    if ref is None:
        ref = next(iter(industries.values()))
    ref = ref[ref["date"] >= pd.Timestamp(start)]
    week_keys = ref["date"].dt.strftime("%G-%V")  # ISO 年-周
    eval_dates = ref.groupby(week_keys)["date"].max().tolist()

    prev = {"state": None, "state_streak": 0, "strong_cand_streak": 0, "vacuum_cand_streak": 0, "prev_top5": []}
    rows = []
    for d in eval_dates:
        snap = compute_snapshot(industries, indices, prev_top5=prev["prev_top5"], as_of=d.date())
        if snap is None:
            continue
        state, streaks, reasons = classify(snap, prev)
        prev = update_state(snap, state, streaks, prev)
        rows.append({
            "date": snap["data_date"],
            "state": state,
            "dominant": dominant_style(snap),
            "spread": snap["spread"],
            "retention": snap["retention"],
            "size20": snap["size20"],
        })

    # 汇总统计
    total = len(rows)
    dist: Dict[str, int] = {}
    for r in rows:
        dist[r["state"]] = dist.get(r["state"], 0) + 1
    transitions = []
    for a, b in zip(rows, rows[1:]):
        if a["state"] != b["state"]:
            transitions.append(f"{a['date']} {STATE_LABELS.get(a['state'], a['state'])} → {STATE_LABELS.get(b['state'], b['state'])}")

    lines = []
    lines.append("# 🔬 风格状态回测")
    lines.append(f"**{rows[0]['date'] if rows else 'N/A'} → {rows[-1]['date'] if rows else 'N/A'}，共 {total} 周**")
    lines.append("")
    lines.append("## 状态分布")
    lines.append("")
    for s in (S_STRONG, S_FORMING, S_VACUUM, S_FADING):
        if s in dist:
            lines.append(f"- {STATE_LABELS[s]}: {dist[s]} 周（{dist[s] / total:.0%}）")
    lines.append("")
    lines.append("## 状态切换时间线")
    lines.append("")
    if transitions:
        lines.extend(f"- {t}" for t in transitions)
    else:
        lines.append("- 无切换")
    lines.append("")
    lines.append("## 逐周明细")
    lines.append("")
    lines.append("| 周 | 状态 | 主导风格 | spread | 重合率 | 大小盘20d |")
    lines.append("|----|------|---------|--------|--------|----------|")
    for r in rows:
        ret_txt = "-" if r["retention"] is None else f"{r['retention']:.0%}"
        size_txt = "-" if r["size20"] is None else f"{r['size20']:+.1f}%"
        lines.append(
            f"| {r['date']} | {STATE_LABELS.get(r['state'], r['state'])} | {r['dominant']} "
            f"| {r['spread']:+.1f}% | {ret_txt} | {size_txt} |"
        )
    lines.append("")
    return "\n".join(lines)
