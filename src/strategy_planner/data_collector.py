# -*- coding: utf-8 -*-
"""
===================================
市场数据采集器
===================================

收集以下类别的数据供 LLM 分析：
1. 宽基指数近期表现（A股 + 港股 + 美股）
2. 申万行业板块周度涨跌排行
3. 宏观因子（国债利率、北向资金、两融余额）
4. 市场情绪（成交量变化、涨停家数走势）
5. 现有策略运行状态摘要
"""
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional, Any

import pandas as pd

logger = logging.getLogger(__name__)

# ── 指数代码映射 ──
A_INDEX_MAP = {
    "上证指数": "000001",
    "深证成指": "399001",
    "创业板指": "399006",
    "科创50": "000688",
    "中证500": "000905",
    "中证1000": "000852",
}

HK_INDEX_MAP = {
    "恒生指数": "HSI",
    "恒生科技": "HSTECH",
}

US_INDEX_MAP = {
    "标普500": "SPX",
    "纳斯达克": "IXIC",
}

# ── 申万一级行业（akshare 行业代码） ──
SW_INDUSTRIES = [
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰",
    "电力设备", "国防军工", "计算机", "传媒", "通信", "银行", "非银金融",
    "机械设备", "煤炭", "石油石化", "环保", "美容护理",
]


def _fetch_index_data(symbol: str, name: str, market: str = "A") -> Optional[pd.DataFrame]:
    """获取指数日线数据"""
    try:
        import akshare as ak
        if market == "A":
            df = ak.stock_zh_index_daily(symbol=f"sh{symbol}" if symbol.startswith("000") else f"sz{symbol}")
        else:
            return None
        if df is not None and len(df) > 0:
            df = df.sort_values("date").reset_index(drop=True)
            return df
    except Exception as e:
        logger.warning(f"获取指数 {name}({symbol}) 数据失败: {e}")
    return None


def collect_index_performance(lookback_days: int = 30) -> Dict[str, Any]:
    """收集宽基指数近期表现"""
    result = {}

    for name, code in A_INDEX_MAP.items():
        df = _fetch_index_data(code, name)
        if df is None or len(df) < 5:
            result[name] = {"error": "数据不足"}
            continue

        close = df["close"].values
        dates = df["date"].values

        info = {
            "latest_close": float(close[-1]),
            "latest_date": str(dates[-1]),
            "returns": {},
        }

        for period, days in [("周", 5), ("两周", 10), ("月", 20)]:
            if len(close) > days:
                info["returns"][period] = f"{float((close[-1] - close[-days - 1]) / close[-days - 1] * 100):.2f}%"
            else:
                info["returns"][period] = "N/A"

        # 均线位置
        for ma_name, ma_days in [("MA5", 5), ("MA10", 10), ("MA20", 20), ("MA60", 60)]:
            if len(close) >= ma_days:
                ma_val = pd.Series(close).rolling(ma_days).mean().iloc[-1]
                bias = float((close[-1] - ma_val) / ma_val * 100)
                info[f"bias_{ma_name}"] = f"{bias:+.2f}%"
            else:
                info[f"bias_{ma_name}"] = "N/A"

        result[name] = info

    # 港股
    try:
        import akshare as ak
        hsi = ak.stock_hk_index_daily_em(symbol="HSI")
        if hsi is not None and len(hsi) >= 5:
            hsi = hsi.sort_values("date").reset_index(drop=True)
            close = hsi["close"].values
            result["恒生指数"] = {
                "latest_close": float(close[-1]),
                "latest_date": str(hsi["date"].values[-1]),
                "returns": {
                    "周": f"{float((close[-1] - close[-6]) / close[-6] * 100) if len(close) >= 6 else 0:.2f}%",
                    "月": f"{float((close[-1] - close[-21]) / close[-21] * 100) if len(close) >= 21 else 0:.2f}%",
                },
            }
    except Exception as e:
        logger.warning(f"获取恒生指数失败: {e}")

    # 美股
    try:
        import yfinance as yf
        for name, ticker in [("标普500", "^GSPC"), ("纳斯达克", "^IXIC")]:
            sp = yf.download(ticker, period="1mo", progress=False)
            if sp is not None and len(sp) >= 5:
                close = sp["Close"].values
                result[name] = {
                    "latest_close": float(close[-1]),
                    "returns": {
                        "周": f"{float((close[-1] - close[-6]) / close[-6] * 100) if len(close) >= 6 else 0:.2f}%",
                        "月": f"{float((close[-1] - close[-21]) / close[-21] * 100) if len(close) >= 21 else 0:.2f}%",
                    },
                }
    except Exception as e:
        logger.warning(f"获取美股指数失败: {e}")

    return result


def collect_sector_performance() -> List[Dict[str, Any]]:
    """收集申万行业周度涨跌排行"""
    try:
        import akshare as ak
        # 申万行业指数日线行情
        today = date.today()
        sw_df = ak.sw_index_daily(indicator="日涨跌幅")
        if sw_df is not None and not sw_df.empty:
            sw_df = sw_df.sort_values("trade_date").reset_index(drop=True)
            if len(sw_df) >= 5:
                recent = sw_df.tail(30)
                sectors = []
                for name in SW_INDUSTRIES:
                    row = recent[recent["index_name"] == name]
                    if row.empty:
                        continue
                    closes = row["close"].values
                    if len(closes) >= 5:
                        week_ret = float((closes[-1] - closes[-5]) / closes[-5] * 100)
                    else:
                        week_ret = 0.0
                    sectors.append({
                        "name": name,
                        "week_return": f"{week_ret:+.2f}%",
                    })
                sectors.sort(key=lambda x: float(x["week_return"].rstrip("%")), reverse=True)
                return sectors
    except Exception as e:
        logger.warning(f"获取行业板块数据失败: {e}")

    return []


def collect_macro_factors() -> Dict[str, Any]:
    """收集宏观因子"""
    factors = {}

    # 国债收益率
    try:
        import akshare as ak
        bond_df = ak.bond_china_yield(start_date=(date.today() - timedelta(days=10)).strftime("%Y-%m-%d"))
        if bond_df is not None and not bond_df.empty:
            latest = bond_df.iloc[-1]
            factors["十年期国债收益率"] = f"{float(latest.get('中国国债收益率10年', 0)):.4f}%"
    except Exception:
        factors["十年期国债收益率"] = "获取失败"

    # 北向资金
    try:
        import akshare as ak
        north = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
        if north is not None and not north.empty:
            north = north.sort_values("date").reset_index(drop=True)
            recent = north.tail(5)
            weekly_flow = float(recent["value"].sum())
            factors["北向资金近5日净流入"] = f"{weekly_flow:.2f}亿元" if abs(weekly_flow) < 10000 else f"{weekly_flow/1e8:.2f}万亿元"
    except Exception:
        factors["北向资金近5日净流入"] = "获取失败"

    # 两融余额
    try:
        import akshare as ak
        margin = ak.stock_margin_detail_sse(date=date.today().strftime("%Y%m%d"))
        if margin is not None and not margin.empty:
            total = margin["margin_balance"].sum()
            factors["两融余额"] = f"{total/1e8:.0f}亿"
    except Exception:
        try:
            margin = ak.stock_margin_sz_date = date.today().strftime("%Y-%m-%d")
            df = ak.stock_margin_detail_sz(date=date.today().strftime("%Y-%m-%d"))
            if df is not None and not df.empty:
                factors["两融余额"] = f"{float(df['rzrqye'].sum())/1e8:.0f}亿"
        except Exception:
            factors["两融余额"] = "获取失败"

    return factors


def collect_market_sentiment() -> Dict[str, Any]:
    """收集市场情绪指标"""
    sentiment = {}

    # 成交量/成交额变化
    try:
        from data_provider.base import DataFetcherManager
        fm = DataFetcherManager()
        stats = fm.get_market_stats()
        if stats:
            sentiment["两市成交额"] = f"{stats.get('total_amount', 0):.0f}亿"
    except Exception:
        sentiment["两市成交额"] = "获取失败"

    # 涨停家数
    try:
        import akshare as ak
        today_str = date.today().strftime("%Y%m%d")
        zt_df = ak.stock_zt_pool_em(date=today_str)
        if zt_df is not None and not zt_df.empty:
            sentiment["涨停家数"] = str(len(zt_df))
        dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
        if dt_df is not None:
            sentiment["跌停家数"] = str(len(dt_df))
    except Exception:
        sentiment["涨停家数"] = "获取失败"

    return sentiment


def collect_market_gate_status() -> Dict[str, Any]:
    """获取现有系统的市场门控状态"""
    try:
        from src.analysis.market_gate import check_market_gate
        can_trade, conditions, summary, regime, hard_intercept = check_market_gate()
        return {
            "can_trade": can_trade,
            "regime": regime,
            "hard_intercept": hard_intercept,
            "conditions": {k: v for k, v in conditions.items()},
            "summary": summary,
        }
    except Exception as e:
        logger.warning(f"获取市场门控状态失败: {e}")
        return {"error": str(e)}


def collect_all_data() -> Dict[str, Any]:
    """收集所有周度数据，返回给 LLM 的结构化数据"""
    logger.info("开始采集周度市场数据...")

    data = {
        "采集时间": date.today().isoformat(),
        "宽基指数表现": collect_index_performance(),
        "行业板块周度排行": collect_sector_performance(),
        "宏观因子": collect_macro_factors(),
        "市场情绪": collect_market_sentiment(),
        "市场门控状态": collect_market_gate_status(),
    }

    logger.info("数据采集完成")
    return data


def format_data_for_llm(data: Dict[str, Any]) -> str:
    """将采集到的数据格式化为 LLM 易读的文本"""
    lines = [f"## 周度市场数据总览 ({data.get('采集时间', 'N/A')})", ""]

    # 1. 宽基指数
    lines.append("### 一、宽基指数近期表现")
    indices = data.get("宽基指数表现", {})
    for name, info in indices.items():
        if isinstance(info, dict) and "latest_close" in info:
            ret = info.get("returns", {})
            ret_str = " | ".join(f"{k}: {v}" for k, v in ret.items())
            bias_parts = []
            for k, v in info.items():
                if k.startswith("bias_"):
                    bias_parts.append(f"{k.replace('bias_', '')}: {v}")
            bias_str = " | ".join(bias_parts) if bias_parts else ""
            lines.append(f"- **{name}**: 收盘 {info['latest_close']:.0f} | {ret_str}")
            if bias_str:
                lines.append(f"  均线偏离: {bias_str}")
    lines.append("")

    # 2. 行业板块
    lines.append("### 二、行业板块周度涨跌排行")
    sectors = data.get("行业板块周度排行", [])
    if sectors:
        top5 = sectors[:5]
        bottom5 = sectors[-5:]
        for i, s in enumerate(top5, 1):
            lines.append(f"- 🟢 TOP{i}: {s['name']} ({s['week_return']})")
        lines.append("  ...")
        for i, s in enumerate(bottom5, len(bottom5)):
            lines.append(f"- 🔴 BOTTOM{i}: {s['name']} ({s['week_return']})")
    else:
        lines.append("- 数据不可用")
    lines.append("")

    # 3. 宏观因子
    lines.append("### 三、宏观因子")
    macro = data.get("宏观因子", {})
    for k, v in macro.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    # 4. 市场情绪
    lines.append("### 四、市场情绪")
    sentiment = data.get("市场情绪", {})
    for k, v in sentiment.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    # 5. 门控状态
    lines.append("### 五、现有系统市场门控状态")
    gate = data.get("市场门控状态", {})
    if "error" in gate:
        lines.append(f"- 获取失败: {gate['error']}")
    else:
        lines.append(f"- 市场状态: **{gate.get('regime', 'N/A')}**")
        lines.append(f"- 是否允许开仓: {'是' if gate.get('can_trade') else '否'}")
        lines.append(f"- 硬拦截: {'触发' if gate.get('hard_intercept') else '无'}")
        for cond_name, cond_val in (gate.get("conditions") or {}).items():
            lines.append(f"  - {'✅' if cond_val else '❌'} {cond_name}")

    return "\n".join(lines)
