# -*- coding: utf-8 -*-
"""
===================================
AmazingData 因子封装层（ETF 周度观察专用）
===================================

从星耀数智 AmazingData 平台获取 ETF 周度观察所需的因子数据：
- 行业 PE/PB 历史分位（申万一级行业，2000 年至今）
- 行业市值占比（拥挤度因子）
- 10 年期国债收益率（股债性价比）

设计原则：
- 所有函数失败均返回 None/空值，绝不抛出异常 —— 调用方据此降级回旧逻辑
- 懒登录，复用 data_provider.amazingdata_fetcher 的登录态（单进程一次登录）
- 未配置 TGW 凭证时自动失效（返回 None）
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# AmazingData 本地缓存目录（InfoData 接口用 HDF5 缓存，需 pytables）
LOCAL_DATA_DIR = os.getenv("AMAZING_DATA_DIR", "D://AmazingData_local_data//")

# 一级行业 PE/PB 分位回看窗口（5 年 ≈ 1250 交易日）
PERCENTILE_LOOKBACK = 1250


def _info() -> Optional[object]:
    """获取 InfoData 实例，失败返回 None。"""
    try:
        from data_provider.amazingdata_fetcher import AmazingDataFetcher

        return AmazingDataFetcher.get_info_data()
    except Exception as e:
        logger.debug(f"AmazingData 不可用（降级）: {e}")
        return None


def _is_available() -> bool:
    """AmazingData 是否可用（TGW 凭证是否配置）。"""
    try:
        from data_provider.amazingdata_fetcher import tgw_configured

        return tgw_configured()
    except Exception:
        return False


# ── 行业基础数据 ──

_industry_base_cache: Optional[pd.DataFrame] = None


def get_industry_base() -> Optional[pd.DataFrame]:
    """
    获取申万行业指数基本信息（INDEX_CODE / LEVEL1_NAME / LEVEL2_NAME / LEVEL3_NAME）。

    Returns:
        DataFrame 或 None（降级）
    """
    global _industry_base_cache
    if _industry_base_cache is not None:
        return _industry_base_cache

    info = _info()
    if info is None:
        return None
    try:
        df = info.get_industry_base_info()
        if df is None or df.empty:
            return None
        _industry_base_cache = df
        return df
    except Exception as e:
        logger.warning(f"获取行业指数基础信息失败: {e}")
        return None


def get_level1_industries() -> List[dict]:
    """
    获取申万一级行业列表。

    Returns:
        [{'code': '801180.SI', 'name': '非银金融'}, ...] 或 []
    """
    base = get_industry_base()
    if base is None:
        return []
    try:
        # LEVEL_TYPE=1 为一级行业；个别字段可能缺失，做兼容
        col_type = "LEVEL_TYPE" if "LEVEL_TYPE" in base.columns else None
        col_name = "LEVEL1_NAME" if "LEVEL1_NAME" in base.columns else None
        if col_type is None or col_name is None:
            return []
        lvl1 = base[base[col_type] == 1]
        seen = {}
        for _, row in lvl1.iterrows():
            name = str(row.get(col_name, "")).strip()
            code = str(row.get("INDEX_CODE", "")).strip()
            if name and code and name not in seen:
                seen[name] = code
        return [{"code": c, "name": n} for n, c in seen.items()]
    except Exception as e:
        logger.warning(f"解析一级行业列表失败: {e}")
        return []


def get_industry_code_by_name(name: str) -> Optional[str]:
    """按行业名（一级行业）查找行业指数代码。"""
    for item in get_level1_industries():
        if item["name"] == name:
            return item["code"]
    return None


# ── 行业日线（含 PE/PB/市值） ──

_industry_daily_cache: Dict[str, pd.DataFrame] = {}


def get_industry_daily(code: str) -> Optional[pd.DataFrame]:
    """
    获取单个行业指数的日线数据（含 PE/PB/总市值/流通市值）。

    Returns:
        DataFrame（升序，列含 CLOSE/PE/PB/TOTAL_CAP/A_FLOAT_CAP）或 None
    """
    if code in _industry_daily_cache:
        return _industry_daily_cache[code]

    info = _info()
    if info is None:
        return None
    try:
        daily = info.get_industry_daily([code], local_path=LOCAL_DATA_DIR, is_local=False)
        df = daily.get(code)
        if df is None or df.empty:
            return None
        df = df.sort_index().dropna(subset=["CLOSE"])
        _industry_daily_cache[code] = df
        return df
    except Exception as e:
        logger.warning(f"获取行业日线 {code} 失败: {e}")
        return None


# ── 因子计算 ──

def get_industry_pe_percentile(code: str, lookback: int = PERCENTILE_LOOKBACK) -> Optional[dict]:
    """
    行业 PE 历史分位（默认近 5 年）。

    Returns:
        {'pe': 当前PE, 'pe_pct': 分位(0-100)} 或 None
    """
    df = get_industry_daily(code)
    if df is None or "PE" not in df.columns:
        return None
    try:
        pe_series = pd.to_numeric(df["PE"], errors="coerce").dropna()
        recent = pe_series.tail(lookback)
        if len(recent) < 250:
            return None
        current_pe = float(recent.iloc[-1])
        if current_pe <= 0:
            return None
        pct = float((recent <= current_pe).mean() * 100)
        return {"pe": round(current_pe, 2), "pe_pct": round(pct, 1)}
    except Exception as e:
        logger.warning(f"计算行业 {code} PE 分位失败: {e}")
        return None


def get_industry_pb_percentile(code: str, lookback: int = PERCENTILE_LOOKBACK) -> Optional[dict]:
    """行业 PB 历史分位（默认近 5 年）。"""
    df = get_industry_daily(code)
    if df is None or "PB" not in df.columns:
        return None
    try:
        pb_series = pd.to_numeric(df["PB"], errors="coerce").dropna()
        recent = pb_series.tail(lookback)
        if len(recent) < 250:
            return None
        current_pb = float(recent.iloc[-1])
        if current_pb <= 0:
            return None
        pct = float((recent <= current_pb).mean() * 100)
        return {"pb": round(current_pb, 2), "pb_pct": round(pct, 1)}
    except Exception as e:
        logger.warning(f"计算行业 {code} PB 分位失败: {e}")
        return None


def get_industry_mcap_share(code: str, lookback: int = PERCENTILE_LOOKBACK) -> Optional[dict]:
    """
    行业市值占比及历史分位（拥挤度因子）。

    市值占比 = 行业总市值 / 全部一级行业总市值之和。
    占比处于历史高位 → 资金拥挤，风险上升。

    Returns:
        {'share': 当前占比(0-1), 'share_pct': 占比历史分位(0-100)} 或 None
    """
    df = get_industry_daily(code)
    if df is None or "TOTAL_CAP" not in df.columns:
        return None
    try:
        cap = pd.to_numeric(df["TOTAL_CAP"], errors="coerce")
        recent = cap.dropna().tail(lookback)
        if len(recent) < 250:
            return None

        # 全行业市值需要总盘子，用行业指数当日市值比价口径：
        # 若拿不到全行业汇总，退化为该行业自身市值序列的分位（趋势拥挤度）
        current_cap = float(recent.iloc[-1])
        if current_cap <= 0:
            return None
        cap_pct = float((recent <= current_cap).mean() * 100)
        return {"share": round(current_cap, 2), "share_pct": round(cap_pct, 1)}
    except Exception as e:
        logger.warning(f"计算行业 {code} 市值占比失败: {e}")
        return None


def get_treasury_yield_y10() -> Optional[float]:
    """
    获取 10 年期国债收益率（%）。

    Returns:
        float（如 2.13）或 None
    """
    info = _info()
    if info is None:
        return None
    try:
        ty = info.get_treasury_yield(["y10"], local_path=LOCAL_DATA_DIR, is_local=False)
        df = ty.get("y10")
        if df is None or df.empty or "YIELD" not in df.columns:
            return None
        val = float(pd.to_numeric(df["YIELD"], errors="coerce").dropna().iloc[-1])
        return val
    except Exception as e:
        logger.warning(f"获取 10 年期国债收益率失败: {e}")
        return None


def get_all_industry_factors(industry_names: List[str]) -> dict:
    """
    批量获取多个行业的因子（PE/PB 分位 + 市值分位）。

    Args:
        industry_names: 一级行业中文名列表，如 ['电子', '医药生物']

    Returns:
        {
            '电子': {'pe': ..., 'pe_pct': ..., 'pb': ..., 'pb_pct': ..., 'share_pct': ...},
            ...
        }（失败的行业不在结果中）
    """
    result = {}
    for name in industry_names:
        code = get_industry_code_by_name(name)
        if code is None:
            logger.warning(f"未找到行业 [{name}] 的指数代码，跳过")
            continue
        factors = {}
        pe_info = get_industry_pe_percentile(code)
        if pe_info:
            factors.update(pe_info)
        pb_info = get_industry_pb_percentile(code)
        if pb_info:
            factors.update(pb_info)
        mcap_info = get_industry_mcap_share(code)
        if mcap_info:
            factors.update(mcap_info)
        if factors:
            result[name] = factors
    return result


# 常见 ETF → 申万一级行业映射（供调用方快速查行业）
ETF_INDUSTRY_MAP = {
    "515010": "非银金融",     # 证券ETF
    "159363": "计算机",       # 创业板人工智能ETF
    "513120": "医药生物",     # 港股创新药
    "159206": "国防军工",     # 卫星ETF
    "588170": "电子",         # 科创半导体ETF
    "159938": "医药生物",     # 医药ETF
    "159928": "食品饮料",     # 消费ETF
    "516560": "社会服务",     # 养老ETF
    # 宽基/策略 ETF 不映射具体行业，走全市场 PE：
    # 563360(A500) / 159680(中证1000增强) / 515180(红利)
}


def get_etf_industry(etf_code: str) -> Optional[str]:
    """查询 ETF 对应的一级行业名。"""
    return ETF_INDUSTRY_MAP.get(str(etf_code).zfill(6))


# ── 全市场聚合 PE（兜底用） ──

def get_market_pe(lookback: int = PERCENTILE_LOOKBACK) -> Optional[dict]:
    """
    用 31 个申万一级行业 PE 市值加权聚合出全市场 PE 及历史分位。

    加权口径：E/P 加权（PE_market = Σ市值 / Σ(市值/PE)），
    避免高 PE 行业被市值权重过分放大。

    Returns:
        {'pe': 当前PE, 'pe_pct': 分位(0-100)} 或 None
    """
    industries = get_level1_industries()
    if not industries:
        return None

    # 逐行业拉日线，对齐交易日索引
    cap_dfs = []
    pe_dfs = []
    for item in industries:
        df = get_industry_daily(item["code"])
        if df is None or "PE" not in df.columns or "TOTAL_CAP" not in df.columns:
            continue
        cap = pd.to_numeric(df["TOTAL_CAP"], errors="coerce")
        pe = pd.to_numeric(df["PE"], errors="coerce")
        # 过滤无效 PE 与市值
        mask = (pe > 0) & (cap > 0)
        cap_dfs.append(cap[mask])
        pe_dfs.append(pe[mask])
    if not cap_dfs:
        return None

    total_cap = pd.concat(cap_dfs, axis=1).sum(axis=1, skipna=False)
    # Σ(市值/PE) 按行业加权
    weighted = []
    for item, cap, pe in zip(industries, cap_dfs, pe_dfs):
        valid = cap.notna() & pe.notna() & (pe > 0) & (cap > 0)
        weighted.append((cap[valid] / pe[valid]).reindex(cap.index))
    sum_ep = pd.concat(weighted, axis=1).sum(axis=1, skipna=False)

    market_pe = total_cap / sum_ep
    market_pe = market_pe[market_pe > 0].dropna()
    recent = market_pe.tail(lookback)
    if len(recent) < 250:
        return None
    current = float(recent.iloc[-1])
    pct = float((recent <= current).mean() * 100)
    return {"pe": round(current, 2), "pe_pct": round(pct, 1)}


# ── ETF 买入优先级 & 卖出警示 ──

# 海外 ETF（暂无 PE 数据，标"数据缺失"）
_OVERSEAS_CODES = frozenset({"513100", "513500", "513380"})


def _etf_pe_info(etf_code: str) -> Optional[dict]:
    """获取单只 ETF 的 PE 信息（行业 PE → 市场 PE → 海外无数据）。"""
    code = str(etf_code).zfill(6)

    if code in _OVERSEAS_CODES:
        return {"source_type": "overseas", "pe": None, "pe_pct": None, "source_name": "海外"}

    industry = get_etf_industry(code)
    if industry:
        ind_code = get_industry_code_by_name(industry)
        if ind_code:
            pe_info = get_industry_pe_percentile(ind_code)
            if pe_info:
                return {**pe_info, "source_type": "industry", "source_name": industry}

    market_pe = get_market_pe()
    if market_pe:
        return {**market_pe, "source_type": "market", "source_name": "全市场"}

    return None


def rank_buy_priorities(etf_list) -> list:
    """按估值便宜度（PE 分位升序）排列 ETF 买入优先级。"""
    results = []
    for etf in etf_list:
        info = _etf_pe_info(etf.code)
        pe_pct = info.get("pe_pct") if info else None
        if pe_pct is None:
            level, level_text = "", "— 无数据"
        elif pe_pct < 20:
            level, level_text = "⭐⭐⭐", "极度低估，优先关注"
        elif pe_pct < 40:
            level, level_text = "⭐⭐", "低估，值得关注"
        elif pe_pct < 60:
            level, level_text = "⭐", "估值合理偏低"
        elif pe_pct < 80:
            level, level_text = "", "中性偏贵，暂缓"
        elif pe_pct < 90:
            level, level_text = "", "偏贵，暂缓"
        else:
            level, level_text = "", "❌ 高估，回避"
        results.append({
            "code": etf.code, "name": etf.name,
            "pe": info.get("pe") if info else None,
            "pe_pct": pe_pct,
            "source_name": info.get("source_name", "") if info else "",
            "level": level, "level_text": level_text,
        })

    results.sort(key=lambda x: (x["pe_pct"] is None, x["pe_pct"] if x["pe_pct"] is not None else 999))
    return results


def check_sell_warnings(etf_list) -> list:
    """
    检查需要卖出警示的 ETF。

    触发条件（三者同时满足才警示）：
    1. 全市场 PE > 90% 分位
    2. 该 ETF 对应行业 PE > 95% 分位
    3. 行业市值拥挤度 > 98% 分位（或行业 PE > 98% 分位）

    海外 ETF 不检查。
    """
    market_pe = get_market_pe()
    if not market_pe or market_pe["pe_pct"] < 90:
        return []

    warnings = []
    for etf in etf_list:
        code = str(etf.code).zfill(6)
        if code in _OVERSEAS_CODES:
            continue

        info = _etf_pe_info(code)
        if not info or info.get("pe_pct") is None:
            continue
        pe_pct = info["pe_pct"]
        if pe_pct < 95:
            continue

        industry = get_etf_industry(code)
        crowding = False
        if industry:
            ind_code = get_industry_code_by_name(industry)
            if ind_code:
                mcap = get_industry_mcap_share(ind_code)
                if mcap and mcap.get("share_pct", 0) > 98:
                    crowding = True

        source = info.get("source_name", "")

        if crowding:
            warnings.append({
                "code": code, "name": etf.name,
                "source_name": source, "pe_pct": pe_pct,
                "reason": f"{source} PE {pe_pct:.0f}%分位 + 市值极度拥挤 → 长期配置建议回避",
            })
        elif pe_pct > 98:
            warnings.append({
                "code": code, "name": etf.name,
                "source_name": source, "pe_pct": pe_pct,
                "reason": f"{source} PE {pe_pct:.0f}%分位（极度高估）→ 长期配置建议回避",
            })

    return warnings
