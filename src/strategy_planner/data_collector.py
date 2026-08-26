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
    """获取指数日线数据（AmazingData 优先，akshare 兜底）"""
    # AmazingData：A 股指数直接支持（000001.SH 等格式）
    if market == "A":
        try:
            from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher
            from AmazingData.utils.constant import Period

            AmazingDataFetcher.ensure_login()
            tgw_code = f"{symbol}.SH" if symbol.startswith("000") else f"{symbol}.SZ"
            kline = AmazingDataFetcher._market_data.query_kline(
                [tgw_code],
                begin_date=20200101,
                end_date=int(date.today().strftime("%Y%m%d")),
                period=Period.day.value,
            )
            df = kline.get(tgw_code)
            if df is not None and not df.empty:
                df = df.rename(columns={"kline_time": "date"})
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
                return df
        except Exception as e:
            logger.debug(f"AmazingData 获取指数 {name} 失败，降级 akshare: {e}")

    try:
        from data_provider.bars import get_index_daily
        if market == "A":
            df = get_index_daily(symbol)
            if df is not None and len(df) > 0:
                return df
        return None
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

    # 港股（em 源优先，sina 源兜底，均失败标注；涨幅不足窗口返回 N/A 而非假 0.00%）
    def _ret_txt(close, days: int) -> str:
        """收盘序列最近 days 个交易日的涨跌幅文本；数据不足返回 N/A。"""
        if len(close) < days + 1:
            return "N/A"
        return f"{float((close[-1] - close[-days - 1]) / close[-days - 1] * 100):.2f}%"

    try:
        import akshare as ak
        hsi = None
        try:
            hsi = ak.stock_hk_index_daily_em(symbol="HSI")
        except Exception as e:
            logger.debug(f"恒生指数 em 源失败，转 sina: {e}")
        if hsi is None or len(hsi) < 5:
            hsi = ak.stock_hk_index_daily_sina(symbol="HSI")
        if hsi is not None and len(hsi) >= 5:
            hsi = hsi.sort_values("date").reset_index(drop=True)
            close = hsi["close"].values
            result["恒生指数"] = {
                "latest_close": float(close[-1]),
                "latest_date": str(hsi["date"].values[-1]),
                "returns": {"周": _ret_txt(close, 5), "月": _ret_txt(close, 21)},
            }
        else:
            result["恒生指数"] = {"error": "数据不可用"}
    except Exception as e:
        logger.warning(f"获取恒生指数失败: {e}")
        result["恒生指数"] = {"error": "数据不可用"}

    # 美股（yfinance 优先，akshare 新浪源兜底，均失败标注；涨幅不足窗口返回 N/A）
    for name, ticker, sina_sym in [("标普500", "^GSPC", ".INX"), ("纳斯达克", "^IXIC", ".IXIC")]:
        close, latest_date = None, None
        try:
            import yfinance as yf
            sp = yf.download(ticker, period="1mo", progress=False)
            if sp is not None and len(sp) >= 5:
                close = sp["Close"].values.flatten()
                latest_date = str(sp.index[-1].date())
        except Exception as e:
            logger.debug(f"美股 {name} yfinance 失败，转 akshare 新浪源: {e}")
        if close is None:
            try:
                import akshare as ak
                sp = ak.index_us_stock_sina(symbol=sina_sym)
                if sp is not None and len(sp) >= 5:
                    sp = sp.tail(30).reset_index(drop=True)
                    close = sp["close"].values.astype(float)
                    latest_date = str(sp["date"].values[-1])
            except Exception as e:
                logger.warning(f"获取美股指数 {name} 失败: {e}")
        if close is not None and len(close) >= 5:
            result[name] = {
                "latest_close": float(close[-1]),
                "latest_date": latest_date,
                "returns": {"周": _ret_txt(close, 5), "月": _ret_txt(close, 21)},
            }
        else:
            result[name] = {"error": "数据不可用"}

    return result


def _tail_percentile(series: pd.Series, lookback: int = 1250) -> Optional[tuple]:
    """序列近 lookback 期的（当前值, 历史分位%）。数据不足返回 None。"""
    recent = pd.to_numeric(series, errors="coerce").dropna().tail(lookback)
    if len(recent) < 250:
        return None
    current = float(recent.iloc[-1])
    return current, float((recent <= current).mean() * 100)


def collect_sector_performance() -> List[Dict[str, Any]]:
    """收集申万行业周度涨跌排行 + 估值分位（AmazingData 优先，akshare 兜底）"""
    # AmazingData：31 个申万一级行业指数日线（含 PE/PB/市值，2000 年至今）
    try:
        from src.etf.amazing_factors import get_level1_industries, get_industry_daily

        sectors = []
        for item in get_level1_industries():
            df = get_industry_daily(item["code"])
            if df is None or len(df) < 6:
                continue
            close = pd.to_numeric(df["CLOSE"], errors="coerce").dropna()
            if len(close) < 6:
                continue
            week_ret = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100)

            entry = {
                "name": item["name"],
                "week_return": f"{week_ret:+.2f}%",
            }

            # 估值维度：PE 分位（近5年）与市值拥挤度
            try:
                current_pe, pe_pct = _tail_percentile(df["PE"])
                if current_pe > 0:
                    entry["pe_pct"] = f"{pe_pct:.0f}%"
                    entry["pe"] = f"{current_pe:.1f}"
            except Exception:
                pass

            try:
                _, cap_pct = _tail_percentile(df["TOTAL_CAP"])
                entry["cap_pct"] = f"{cap_pct:.0f}%"
            except Exception:
                pass

            sectors.append(entry)
        if sectors:
            sectors.sort(key=lambda x: float(x["week_return"].rstrip("%")), reverse=True)
            return sectors
    except Exception as e:
        logger.warning(f"AmazingData 获取行业板块数据失败，降级 akshare: {e}")

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

    # 国债收益率（AmazingData 优先，akshare 兜底）
    try:
        from src.etf.amazing_factors import get_treasury_yield_y10

        y10 = get_treasury_yield_y10()
        if y10 and y10 > 0:
            factors["十年期国债收益率"] = f"{y10:.4f}%"
        else:
            raise ValueError("AmazingData 未返回有效收益率")
    except Exception:
        try:
            import akshare as ak
            bond_df = ak.bond_china_yield(start_date=(date.today() - timedelta(days=10)).strftime("%Y-%m-%d"))
            if bond_df is not None and not bond_df.empty:
                latest = bond_df.iloc[-1]
                factors["十年期国债收益率"] = f"{float(latest.get('中国国债收益率10年', 0)):.4f}%"
            else:
                factors["十年期国债收益率"] = "获取失败"
        except Exception:
            factors["十年期国债收益率"] = "获取失败"

    # 两融余额（AmazingData 优先，akshare 兜底）
    try:
        from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher

        info = AmazingDataFetcher.get_info_data()
        margin = info.get_margin_summary(is_local=False)
        if margin is not None and not margin.empty:
            # 各交易所更新日期不同步：按交易所分别取各自最新日，再汇总沪深两所
            if "TRADE_DATE" in margin.columns and "EXCHANGE" in margin.columns:
                margin = margin[margin["EXCHANGE"].isin(["SSE", "SZSE"])]
                if not margin.empty:
                    margin = (
                        margin.sort_values("TRADE_DATE")
                        .groupby("EXCHANGE", as_index=False)
                        .tail(1)
                    )
            total = float(margin["SUM_BORROW_MONEY_BALANCE"].sum())
            factors["两融余额"] = f"{total/1e8:.0f}亿"
        else:
            raise ValueError("AmazingData 未返回两融数据")
    except Exception as e:
        logger.debug(f"AmazingData 获取两融余额失败，降级 akshare: {e}")
        try:
            import akshare as ak
            margin = ak.stock_margin_detail_sse(date=date.today().strftime("%Y%m%d"))
            if margin is not None and not margin.empty:
                total = margin["margin_balance"].sum()
                factors["两融余额"] = f"{total/1e8:.0f}亿"
            else:
                factors["两融余额"] = "获取失败"
        except Exception:
            factors["两融余额"] = "获取失败"

    return factors


def collect_market_sentiment() -> Dict[str, Any]:
    """收集市场情绪指标（当日快照 + 近5日序列）"""
    sentiment = {}

    # 两市成交额（当日）
    try:
        from data_provider import DataFetcherManager
        fm = DataFetcherManager()
        stats = fm.get_market_stats()
        if stats:
            sentiment["两市成交额"] = f"{stats.get('total_amount', 0):.0f}亿"
    except Exception:
        sentiment["两市成交额"] = "获取失败"

    # 上证成交额近5日序列（用上证指数日线 amount 作趋势代理）
    try:
        from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher
        from AmazingData.utils.constant import Period

        AmazingDataFetcher.ensure_login()
        kline = AmazingDataFetcher._market_data.query_kline(
            ["000001.SH"],
            begin_date=int((date.today() - timedelta(days=30)).strftime("%Y%m%d")),
            end_date=int(date.today().strftime("%Y%m%d")),
            period=Period.day.value,
        )
        df = kline.get("000001.SH")
        if df is not None and not df.empty:
            df = df.sort_values("kline_time").tail(5)
            amt_series = (pd.to_numeric(df["amount"], errors="coerce") / 1e8).round(0).astype(int)
            sentiment["上证成交额近5日(亿)"] = " → ".join(map(str, amt_series.tolist()))
    except Exception as e:
        logger.debug(f"获取上证成交额序列失败: {e}")

    # 涨停/跌停家数（当日 + 近5日序列）
    try:
        import akshare as ak
        from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher

        # 用交易日历取最近 5 个交易日
        trade_dates = []
        try:
            AmazingDataFetcher.ensure_login()
            cal = AmazingDataFetcher._calendar
            if cal:
                trade_dates = [str(d) for d in cal[-5:]]
        except Exception:
            pass
        if not trade_dates:
            trade_dates = [date.today().strftime("%Y%m%d")]

        zt_series, dt_series = [], []
        for d in trade_dates:
            try:
                zt_df = ak.stock_zt_pool_em(date=d)
                zt_series.append(len(zt_df) if zt_df is not None else 0)
            except Exception:
                zt_series.append(-1)
            try:
                dt_df = ak.stock_zt_pool_dtgc_em(date=d)
                dt_series.append(len(dt_df) if dt_df is not None else 0)
            except Exception:
                dt_series.append(-1)

        if zt_series and zt_series[-1] >= 0:
            sentiment["涨停家数"] = str(zt_series[-1])
        if dt_series and dt_series[-1] >= 0:
            sentiment["跌停家数"] = str(dt_series[-1])
        if len(zt_series) >= 3:
            sentiment["涨停家数近5日"] = " → ".join(str(v) if v >= 0 else "N/A" for v in zt_series)
        if len(dt_series) >= 3:
            sentiment["跌停家数近5日"] = " → ".join(str(v) if v >= 0 else "N/A" for v in dt_series)
    except Exception:
        if "涨停家数" not in sentiment:
            sentiment["涨停家数"] = "获取失败"

    return sentiment


def collect_market_news() -> List[Dict[str, str]]:
    """收集本周市场重要资讯（妙想金融资讯搜索）"""
    try:
        from src.mx.service import MXService

        service = MXService()
        queries = [
            "本周A股市场重要新闻",
            "本周宏观政策 货币政策 财政政策",
            "本周市场热点板块",
        ]
        news_map = {}  # title -> item，跨查询去重
        for q in queries:
            try:
                resp = service.search_news(q)
                if not resp:
                    continue
                items = (
                    resp.get("data", {})
                    .get("data", {})
                    .get("llmSearchResponse", {})
                    .get("data", [])
                )
                for item in items:
                    title = (item.get("title") or "").strip()
                    content = (item.get("content") or "").strip()
                    if not title or not content:
                        continue
                    # 只收权威源（L1 级），过滤自媒体/营销号
                    authority = str(item.get("authorityLevel", ""))
                    if authority and not authority.startswith("L1"):
                        continue
                    if title not in news_map:
                        news_map[title] = {
                            "title": title[:80],
                            "content": content[:200],
                            "date": str(item.get("date", ""))[:10],
                            "source": item.get("source", ""),
                        }
            except Exception as e:
                logger.debug(f"资讯查询失败 [{q}]: {e}")
        news = list(news_map.values())
        # 按日期倒序，取前 10 条
        news.sort(key=lambda x: x["date"], reverse=True)
        return news[:10]
    except Exception as e:
        logger.warning(f"收集市场资讯失败: {e}")
        return []


def collect_market_gate_status() -> Dict[str, Any]:
    """获取现有系统的市场门控状态"""
    try:
        from src.analysis.market_gate import check_market_gate, fetch_gate_inputs
        from data_provider import DataFetcherManager
        fm = DataFetcherManager()
        can_trade, conditions, summary, regime, hard_intercept = check_market_gate(fetch_gate_inputs(fm))
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
        "本周重要资讯": collect_market_news(),
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
        elif isinstance(info, dict) and "error" in info:
            lines.append(f"- **{name}**: {info['error']}")
    lines.append("")

    # 2. 行业板块
    lines.append("### 二、行业板块周度涨跌排行（含估值分位）")
    sectors = data.get("行业板块周度排行", [])
    if sectors:
        top5 = sectors[:5]
        bottom5 = sectors[-5:]
        for i, s in enumerate(top5, 1):
            extra = f" | PE分位: {s['pe_pct']} | 市值分位: {s['cap_pct']}" if "pe_pct" in s else ""
            lines.append(f"- 🟢 TOP{i}: {s['name']} ({s['week_return']}){extra}")
        lines.append("  ...")
        for i, s in enumerate(bottom5, 1):
            extra = f" | PE分位: {s['pe_pct']} | 市值分位: {s['cap_pct']}" if "pe_pct" in s else ""
            lines.append(f"- 🔴 BOTTOM{i}: {s['name']} ({s['week_return']}){extra}")
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
    lines.append("")

    # 6. 本周重要资讯
    lines.append("### 六、本周重要资讯")
    news = data.get("本周重要资讯", [])
    if news:
        for n in news:
            lines.append(f"- [{n['date']}] **{n['title']}**（{n['source']}）")
            lines.append(f"  {n['content']}")
    else:
        lines.append("- 数据不可用")
    lines.append("")

    return "\n".join(lines)
