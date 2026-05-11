# -*- coding: utf-8 -*-
"""
===================================
趋势跟踪系统（无 LLM 版本）
===================================

定位：趋势波段系统。只做主线中的强趋势股，只在分歧回踩时介入。

职责：
1. 读取妙想自选股，直接进行技术分析
2. 市场环境过滤（满足2/5条件才允许开仓）
3. 纯技术分析（第一次分歧回踩MA5等规则）
4. 模拟交易执行

核心策略：
- 买点：主升中的第一次分歧回踩MA5（缩量 + 不破5日线 + 换手率>5%）
- 不做：加速追高、情绪高潮接力、连续大阳后追涨
- 第一卖点(减仓50%)：放量跌破5日线 / 高位长阴 / 回撤≥5%
- 第二卖点(清仓)：跌破10日线 / 放量跌破10日线
- 环境过滤：满足2/5项市场条件才允许开仓，否则空仓
- 趋势不走坏即保留，连续2天跌破10日线才剔除

使用方式：
    python main.py              # 正常运行
    python main.py --debug      # 调试模式
    python main.py --trade       # 分析+生成交易计划
    python main.py --trade-execute  # 执行盘中交易
    python main.py --trade-plan  # 查看当前交易计划
"""
import argparse
import io
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, time
from typing import List, Dict, Optional, Tuple

import pandas as pd

from data_provider.base import DataFetcherManager, canonical_stock_code
from src.config import setup_env
from src.notification import NotificationService
from src.services.mx_service import MXService
from src.stock_analyzer import StockTrendAnalyzer
from market_review import check_market_gate

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

setup_env()

logger = logging.getLogger(__name__)


@dataclass
class TechnicalSignal:
    """技术信号数据类"""
    code: str
    name: str
    signal_type: str  # ' pullback_ma5', 'breakout', 'oversold' 等
    score: int  # 0-100
    current_price: float
    ma5: float
    ma10: float
    ma20: float
    bias_ma5: float  # 乖离率
    volume_ratio: float  # 量比
    turnover_rate: float  # 换手率
    description: str  # 信号描述


class SimpleTechnicalAnalyzer:
    """
    简化版技术分析器
    
    不依赖 LLM，纯技术指标计算
    """
    
    def __init__(self):
        self.fetcher = DataFetcherManager()
        self.mx_service = MXService()
        self.trend_analyzer = StockTrendAnalyzer()  # 复用 main.py 的技术指标计算
        self._trading_dates_cache = None
    
    def get_trading_dates(self, start_date: date, end_date: date) -> List[date]:
        if self._trading_dates_cache is not None:
            return [d for d in self._trading_dates_cache if start_date <= d <= end_date]
        
        try:
            import akshare as ak
            cal_df = ak.tool_trade_date_hist_sina()
            trading_dates = []
            for _, row in cal_df.iterrows():
                trade_date = pd.to_datetime(row['trade_date']).date()
                if start_date <= trade_date <= end_date:
                    trading_dates.append(trade_date)
            self._trading_dates_cache = trading_dates
            return trading_dates
        except Exception as e:
            # 【修改点】不要再做自然日近似了，直接抛出异常，让主程序报错退出！
            logger.error(f"严重错误：获取交易日历失败！无法进行后续精确计算。错误信息: {e}")
            raise RuntimeError("交易日历获取失败。")

    # check_market_environment 已迁移至 market_review.check_market_gate()

    def should_remove_stock(self, code: str, df: Optional[pd.DataFrame] = None) -> Tuple[bool, str]:
        """
        检查股票是否应该剔除

        剔除规则：
        1. 连续2天收盘跌破10日线
        2. 放量长阴破趋势（单日放量暴跌破位）

        Args:
            code: 股票代码
            df: 可选，已预先拉取的行情数据；传入则跳过内部拉取，避免重复请求

        Returns:
            (是否剔除, 剔除原因)
        """

        try:
            if df is None:
                df = self.fetch_stock_data(code, days=30)
            if df is not None and len(df) >= 10:
                df = df.sort_values('date').reset_index(drop=True)
                df = self.trend_analyzer._calculate_mas(df)

                if len(df) < 2:
                    return False, ""

                latest = df.iloc[-1]
                prev = df.iloc[-2]

                close = latest.get('close')
                prev_close = prev.get('close')
                ma10 = latest.get('ma10')
                prev_ma10 = prev.get('ma10')
                volume = latest.get('volume', 0)
                prev_volume = prev.get('volume', 1)
                volume_ratio = volume / prev_volume if prev_volume > 0 else 1

                # 规则1：连续2天收盘跌破10日线
                if (close is not None and ma10 is not None and not pd.isna(ma10) and
                    prev_close is not None and prev_ma10 is not None and not pd.isna(prev_ma10)):
                    if close < ma10 and prev_close < prev_ma10:
                        return True, f"连续2天收盘跌破10日线（昨{prev_close:.2f}<{prev_ma10:.2f}，今{close:.2f}<{ma10:.2f}）"

                # 规则2：放量长阴破趋势（单日放量暴跌，跌幅≥5%且量比≥2）
                if close is not None and prev_close is not None and prev_close > 0:
                    pct_change = (close - prev_close) / prev_close * 100
                    if (pct_change <= -5 and volume_ratio >= 2 and
                        ma10 is not None and not pd.isna(ma10) and
                        close < ma10):
                        return True, f"放量长阴破趋势（跌幅{pct_change:.1f}%，量比{volume_ratio:.1f}）"

                # 规则3：情绪过热（近5日换手均值 > 近20日均值的2倍）
                if 'turnover_rate' in df.columns and len(df) >= 20:
                    r5  = df['turnover_rate'].iloc[-5:].mean()
                    r20 = df['turnover_rate'].iloc[-20:].mean()
                    if (pd.notna(r5) and pd.notna(r20) and r20 > 0 and r5 > r20 * 2.0):
                        return True, f"情绪过热（近5日换手{r5:.1f}% 是近20日均{r20:.1f}%的{r5/r20:.1f}倍）"

        except Exception as e:
            logger.debug(f"检查 {code} 剔除条件时出错: {e}")

        return False, ""

    def fetch_stock_data(self, code: str, days: int = 30) -> Optional[pd.DataFrame]:
        """
        获取股票历史数据（直接从网络获取）

        Args:
            code: 股票代码
            days: 获取天数

        Returns:
            DataFrame 或 None
        """
        try:
            end_date = date.today()
            start_date = end_date - timedelta(days=days)
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')

            result = self.fetcher.get_daily_data(code, start_str, end_str)
            if isinstance(result, tuple) and len(result) >= 1:
                df = result[0]
            else:
                df = result

            if df is not None and hasattr(df, 'empty') and not df.empty:
                df_latest_date = pd.to_datetime(df['date'].max()).date()
                trading_dates = self.get_trading_dates(end_date - timedelta(days=30), end_date)
                if trading_dates:
                    last_trading_day = trading_dates[-1]
                    if df_latest_date < last_trading_day:
                        logger.error(f"❌ {code} 网络获取的数据仍过期(最新:{df_latest_date}, 需要:{last_trading_day})")
                        return None
                return df
            else:
                logger.error(f"❌ {code} 从网络获取数据失败")
                return None

        except Exception as e:
            logger.warning(f"获取 {code} 数据失败: {e}")
            return None
    
    def detect_signals(self, code: str, name: str, df: pd.DataFrame) -> List[TechnicalSignal]:
        """
        检测技术信号

        趋势波段系统，只做主线中的强趋势股，只在分歧回踩时介入。

        信号优先级：
        1. 缩量回踩 MA5（主升中的第一次像样分歧）— 最佳买点
        2. 缩量回踩 MA10（次优买点，但需谨慎）

        不做：加速追高、情绪高潮接力、连续大阳后追涨。
        """
        signals = []

        if df is None:
            logger.warning(f"⚠️ {name}({code}): 数据为空，跳过分析")
            return signals

        if len(df) < 20:
            logger.warning(f"⚠️ {name}({code}): 数据不足20条(仅{len(df)}条)，可能影响分析准确性")

        df = df.sort_values('date').reset_index(drop=True)
        df = self.trend_analyzer._calculate_mas(df)

        # 取最新数据
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else None

        current_price = latest['close']
        ma5 = latest['ma5']
        ma10 = latest['ma10']
        ma20 = latest['ma20']
        _vr = latest.get('volume_ratio')
        volume_ratio = float(_vr) if _vr is not None and pd.notna(_vr) else 1.0

        if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
            return signals

        # 计算乖离率
        bias_ma5 = (current_price - ma5) / ma5 * 100 if ma5 > 0 else 0
        bias_ma10 = (current_price - ma10) / ma10 * 100 if ma10 > 0 else 0

        # 计算当日涨跌幅
        pct_change = 0.0
        if prev is not None and prev['close'] > 0:
            pct_change = (current_price - prev['close']) / prev['close'] * 100

        # === 策略检查 ===

        # 1. 均线多头排列：5日线 > 10日线 > 20日线
        is_bullish_alignment = ma5 > ma10 > ma20

        # 2. 不破5日线（或盘中破但尾盘收回）
        holds_ma5 = current_price >= ma5 * 0.995  # 允许微破

        # 3. 选股池条件：换手率 > 5%（保证活跃度）
        turnover = latest.get('turnover_rate', 0)
        meets_liquidity = turnover > 5.0

        # 4. 非连续加速状态：检查最近3天涨幅是否逐渐缩小（分歧特征）
        is_accelerating = False
        if len(df) >= 4:
            day1 = (df.iloc[-1]['close'] - df.iloc[-2]['close']) / df.iloc[-2]['close'] * 100
            day2 = (df.iloc[-2]['close'] - df.iloc[-3]['close']) / df.iloc[-3]['close'] * 100
            day3 = (df.iloc[-3]['close'] - df.iloc[-4]['close']) / df.iloc[-4]['close'] * 100
            if day1 > day2 > day3 and day3 > 0:
                is_accelerating = True

        # 5. 缩量检查：环比缩量 + 量比 < 0.8（断崖式缩量特征）
        prev_volume = prev['volume'] if prev is not None else 0
        current_volume = latest['volume']
        is_volume_shrink = (current_volume < prev_volume * 0.7) and (volume_ratio < 0.8)

        # 只有均线多头排列的股票才有分析意义
        if not is_bullish_alignment:
            return signals

        # 信号1: 缩量回踩 MA5（主升中的第一次分歧回踩）— 最佳买点
        # 条件：多头排列 + 守住MA5 + 缩量 + 小实体/小跌 + 换手达标 + 非加速
        if (holds_ma5 and is_volume_shrink and abs(bias_ma5) < 2.0
                and abs(pct_change) < 3.0 and meets_liquidity and not is_accelerating):
            
            # --- 龙头换手标注（借鉴 dragon_head 策略）---
            signal_desc = f"第一次分歧回踩MA5，缩量（量比{volume_ratio:.2f}）不破5日线，涨跌{pct_change:+.2f}%"
            if turnover > 8.0 and 2.0 <= pct_change <= 5.0:
                signal_desc += " ⭐换手活跃具龙头特征"
            elif turnover > 5.0:
                signal_desc += f" 换手{turnover:.1f}%正常"
            
            # --- 一阳三阴形态标注（借鉴 one_yang_three_yin 策略）---
            if len(df) >= 4:
                anchor = df.iloc[-4]
                anchor_pct = (anchor['close'] - anchor['open']) / anchor['open'] * 100 if anchor['open'] > 0 else 0
                if anchor_pct > 3.0:
                    signal_desc += " 📐一阳三阴形态"
                    
            signals.append(TechnicalSignal(
                code=code,
                name=name,
                signal_type='pullback_ma5',
                score=90,
                current_price=current_price,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                bias_ma5=bias_ma5,
                volume_ratio=volume_ratio,
                turnover_rate=turnover,
                description=signal_desc
            ))

        # 信号2: 缩量回踩 MA10（次优买点 — 回踩较深，需确认支撑）
        # 策略强调"不破5日线"，回踩MA10说明分歧较大，评分降低
        elif (is_volume_shrink
              and (-3.0 < bias_ma10 < 1.0)  # 接近或微破MA10
              and abs(pct_change) < 3.0
              and meets_liquidity
              and not is_accelerating
              and not holds_ma5):  # 确实跌破了MA5
            signals.append(TechnicalSignal(
                code=code,
                name=name,
                signal_type='pullback_ma10',
                score=65,  # 策略偏谨慎，回踩MA10评分降低
                current_price=current_price,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                bias_ma5=bias_ma5,
                volume_ratio=volume_ratio,
                turnover_rate=turnover,
                description=f"回踩MA10（回踩较深），缩量（量比{volume_ratio:.2f}），涨跌{pct_change:+.2f}%，需次日弱转强确认"
            ))

        # 注意：不放量突破信号 — 策略明确规定"不做加速追高"

        return signals
    
    def analyze_all_stocks(self, stock_list: List[Tuple[str, str]]) -> Tuple[List[TechnicalSignal], List[Tuple[str, str, str]]]:
        """
        分析所有关注股票，返回技术信号列表和剔除列表

        Args:
            stock_list: [(code, name), ...]

        Returns:
            (技术信号列表, [(code, name, 剔除原因), ...])
        """
        all_signals = []
        removed_stocks = []

        logger.info(f"开始处理 {len(stock_list)} 只股票...")

        for i, (code, name) in enumerate(stock_list):
            try:
                df = self.fetch_stock_data(code)

                should_remove, remove_reason = self.should_remove_stock(code, df=df)
                if should_remove:
                    removed_stocks.append((code, name, remove_reason))
                    logger.info(f"❌ 剔除 {name}({code}): {remove_reason}")
                    continue

                if df is None:
                    continue

                signals = self.detect_signals(code, name, df)
                all_signals.extend(signals)

                if signals:
                    logger.info(f"✅ {name}({code}): 发现 {len(signals)} 个信号")

                if (i + 1) % 10 == 0:
                    logger.info(f"进度: {i + 1}/{len(stock_list)}")

            except Exception as e:
                logger.warning(f"分析 {code} 失败: {e}")
                continue

        all_signals.sort(key=lambda x: x.score, reverse=True)

        kept_count = len(stock_list) - len(removed_stocks)
        logger.info(f"处理完成 | 保留:{kept_count} 剔除:{len(removed_stocks)} 信号:{len(all_signals)}")
        return all_signals, removed_stocks

    def generate_report(self, signals: List[TechnicalSignal], removed_stocks: Optional[List[Tuple[str, str, str]]] = None,
                        market_env: Optional[Tuple] = None) -> str:
        """
        生成 Markdown 格式的报告

        Args:
            signals: 技术信号列表
            removed_stocks: 被剔除的股票列表 [(code, name, reason), ...]
            market_env: 市场环境检查结果 (can_trade, conditions, summary, regime)
        """
        REGIME_DESC = {
            "trending_up":   "📈 趋势上行 — 均线多头，适合积极持有缩量回踩信号",
            "sideways":      "➡️ 震荡横盘 — 降低期望，控制仓位，等待方向明确",
            "trending_down": "📉 趋势下行 — 均线空头，建议轻仓观望，持仓注意止损线",
            "unknown":       "❓ 状态不明 — 保守为主，等待方向明确",
        }
        removed_stocks = removed_stocks or []
        today_str = datetime.now().strftime('%Y-%m-%d')

        lines = [
            f"# 📊 趋势跟踪日报 ({today_str})",
            "",
            f"> 定位：趋势波段系统。只做主线中的强趋势股，只在分歧回踩时介入。",
            "",
            f"> 共发现 **{len(signals)}** 个技术信号 | 剔除 **{len(removed_stocks)}** 只股票",
            "",
            "---",
            "",
        ]

        # 大盘状态栏
        if market_env:
            can_trade = market_env[0]
            conditions = market_env[1]
            regime = market_env[3] if len(market_env) >= 4 else "unknown"
            regime_text = REGIME_DESC.get(regime, "❓ 状态不明")
            env_icon = "✅" if can_trade else "⛔"
            met = sum(1 for v in conditions.values() if v)
            lines.extend([
                "## 🌤️ 市场环境",
                "",
                f"> **【大盘状态】{regime_text}**",
                "",
                f"> **{env_icon} {'允许开仓' if can_trade else '建议空仓'}**（满足{met}/5项条件）",
                "",
            ])
            for cond_name, met_val in conditions.items():
                icon = "✅" if met_val else ("❌" if met_val is not None else "⟖")
                lines.append(f"- {icon} {cond_name}")
            lines.extend(["", "---", ""])

        # 显示剔除的股票
        if removed_stocks:
            lines.extend([
                "## ❌ 剔除股票（趋势破坏）",
                "",
                "| 股票 | 剔除原因 |",
                "|------|----------|",
            ])
            for code, name, reason in removed_stocks[:20]:
                lines.append(f"| {name}({code}) | {reason} |")
            if len(removed_stocks) > 20:
                lines.append(f"| ... | 等共{len(removed_stocks)}只股票 |")
            lines.extend(["", "---", ""])

        # 分类展示
        pullback_ma5_signals = [s for s in signals if s.signal_type == 'pullback_ma5']
        pullback_ma10_signals = [s for s in signals if s.signal_type == 'pullback_ma10']

        # 第一次分歧回踩MA5（高优先级 — 策略首选买点）
        if pullback_ma5_signals:
            lines.extend([
                "## 🎯 第一次分歧回踩MA5（策略首选买点）",
                "",
                "> 只做主升中的第一次像样分歧。缩量回踩，不破5日线。",
                "",
                "| 股票 | 价格 | MA5 | MA10 | 乖离率 | 量比 | 换手率 | 评分 | 描述 |",
                "|------|------|-----|------|--------|------|--------|------|------|",
            ])
            for s in pullback_ma5_signals:
                lines.append(
                    f"| {s.name}({s.code}) | {s.current_price:.2f} | {s.ma5:.2f} | {s.ma10:.2f} | "
                    f"{s.bias_ma5:+.2f}% | {s.volume_ratio:.2f} | {s.turnover_rate:.1f}% | {s.score} | {s.description} |"
                )
            lines.append("")

        # 回踩MA10（次优 — 需谨慎，策略要求不破5日线）
        if pullback_ma10_signals:
            lines.extend([
                "## ⚠️ 回踩MA10（次优 — 需次日弱转强确认）",
                "",
                "> 已跌破5日线，回踩较深。策略要求不破5日线，此信号仅作参考。",
                "",
                "| 股票 | 价格 | MA5 | MA10 | 乖离率MA5 | 量比 | 换手率 | 评分 | 描述 |",
                "|------|------|-----|------|-----------|------|--------|------|------|",
            ])
            for s in pullback_ma10_signals:
                lines.append(
                    f"| {s.name}({s.code}) | {s.current_price:.2f} | {s.ma5:.2f} | {s.ma10:.2f} | "
                    f"{s.bias_ma5:+.2f}% | {s.volume_ratio:.2f} | {s.turnover_rate:.1f}% | {s.score} | {s.description} |"
                )
            lines.append("")

        # 汇总
        lines.extend([
            "---",
            "",
            "## 📈 信号汇总",
            "",
        ])

        for s in signals[:10]:
            emoji = "🟢" if s.score >= 80 else "🟡" if s.score >= 60 else "⚪"
            lines.append(f"{emoji} **{s.name}({s.code})**: {s.description} | 评分:{s.score}")

        lines.extend([
            "",
            "---",
            "",
            "**策略规则**:",
            "- 买点: 主升中的第一次分歧回踩MA5（缩量 + 不破5日线 + 换手率>5%）",
            "- 不做: 加速追高、情绪高潮接力、连续大阳后追涨",
            "- 第一卖点(减仓50%): 放量跌破5日线 / 高位长阴 / 回撤≥5%",
            "- 第二卖点(清仓): 跌破10日线 / 放量跌破10日线",
            "- 环境过滤: 满足2/5项市场条件才允许开仓，否则空仓",
        ])

        return "\n".join(lines)


@dataclass
class BuyCondition:
    """买入条件检查结果"""
    passed: bool
    reason: str
    buy_zone: Optional[Tuple[float, float]] = None


@dataclass
class SellCondition:
    """卖出条件检查结果"""
    should_sell: bool
    reason: str
    sell_price: Optional[float] = None
    sell_ratio: float = 1.0


class TradeDecisionHelper:
    """
    基于《技术分析日报·次日实战交易手册》的决策辅助
    """

    MARKET_OPEN = time(9, 30)
    MORNING_END = time(11, 30)
    AFTERNOON_START = time(13, 0)
    MARKET_CLOSE = time(15, 0)

    BEST_BUY_START = time(9, 40)
    BEST_BUY_END = time(10, 0)
    SAFE_BUY_START = time(14, 30)

    MAX_SINGLE_POSITION = 0.30
    MAX_HOLDINGS = 2
    STOP_LOSS_MA10 = True

    def __init__(self):
        self.signals: List[Dict] = []

    def load_signals(self, signals: List[TechnicalSignal]):
        """加载技术分析信号"""
        self.signals = []
        for s in signals:
            if s.signal_type == 'pullback_ma5' and s.score >= 80:
                self.signals.append({
                    'code': s.code,
                    'name': s.name,
                    'score': s.score,
                    'current_price': s.current_price,
                    'ma5': s.ma5,
                    'ma10': s.ma10,
                    'ma20': s.ma20,
                })
        logger.info(f"加载 {len(self.signals)} 个高优先级买入信号")

    def get_buy_candidates(self) -> List[Dict]:
        """获取买入候选列表（只做缩量回踩MA5，评分≥80）"""
        return [s for s in self.signals if s.get('score', 0) >= 80]

    def check_buy_conditions(self,
                           signal: Dict,
                           current_price: float,
                           current_volume_ratio: float,
                           current_time: datetime,
                           market_status: Dict) -> BuyCondition:
        """检查3个必须买入条件"""
        ma10 = signal.get('ma10')
        ma5 = signal.get('ma5')

        if current_price < ma10 * 0.995:
            return BuyCondition(
                passed=False,
                reason=f"跌破MA10生命线(当前{current_price:.2f} < MA10:{ma10:.2f})"
            )

        if current_volume_ratio > 1.0:
            return BuyCondition(
                passed=False,
                reason=f"放量({current_volume_ratio:.2f} > 1.0)，非缩量洗盘"
            )

        prev_close = signal.get('current_price', current_price)
        change_pct = (current_price - prev_close) / prev_close * 100
        if abs(change_pct) > 3:
            return BuyCondition(
                passed=False,
                reason=f"涨跌幅过大({change_pct:+.2f}%)，超出±3%范围"
            )

        buy_zone = (ma10 * 0.998, ma5 * 1.002) if ma10 and ma5 else None

        return BuyCondition(
            passed=True,
            reason="满足所有买入条件",
            buy_zone=buy_zone
        )

    def check_time_window(self, current_time: datetime) -> Tuple[bool, str]:
        """检查是否在允许买入的时间窗口"""
        t = current_time.time()

        if self.BEST_BUY_START <= t <= self.BEST_BUY_END:
            return True, "激进买入窗口(9:40-10:00)"

        if self.SAFE_BUY_START <= t <= self.MARKET_CLOSE:
            return True, "稳健买入窗口(14:30-15:00)"

        if time(9, 25) <= t < time(9, 30):
            return False, "集合竞价时段，禁止买入"

        return False, f"非最佳买入时段({t.strftime('%H:%M')})"

    def check_risk_conditions(self,
                            market_index_change: float,
                            has_bad_news: bool,
                            holdings_count: int) -> Tuple[bool, str]:
        """检查风险放弃条件"""
        if market_index_change < -2:
            return False, f"系统性风险(大盘跌{market_index_change:.2f}%)"

        if has_bad_news:
            return False, "个股突发利空"

        if holdings_count >= self.MAX_HOLDINGS:
            return False, f"持仓已达上限({holdings_count}/{self.MAX_HOLDINGS})"

        return True, "风险检查通过"

    def check_sell_conditions(self,
                            position: Dict,
                            current_price: float,
                            ma10: float,
                            ma5: float,
                            cost_price: float,
                            volume_ratio: float = 1.0,
                            pct_change: float = 0.0,
                            position_high: Optional[float] = None) -> SellCondition:
        """
        检查卖出条件

        第一卖点（减仓50%）：
        - 放量跌破5日线
        - 高位长阴吞没
        - 从阶段高点回撤≥5%
        - 板块明显走弱

        第二卖点（全仓清仓）：
        - 连续2日收盘跌破10日线（盘中只能检查当日情况）
        - 放量跌破10日线
        - 主线明显退潮
        - 个股跌破关键平台

        止盈保护：盈利≥15%且放量滞涨，主动止盈半仓
        """
        profit_pct = (current_price - cost_price) / cost_price * 100

        # --- 极端止损（盘中直接走）---
        if pct_change <= -7:
            return SellCondition(
                should_sell=True,
                reason=f"极端止损：闪崩暴跌{pct_change:.1f}%",
                sell_price=current_price,
                sell_ratio=1.0
            )

        # --- 第二卖点：全仓清仓 ---
        # 放量跌破10日线（生命线失守）
        if current_price < ma10 * 0.995 and volume_ratio > 1.5:
            return SellCondition(
                should_sell=True,
                reason=f"放量跌破MA10生命线（价{current_price:.2f}<{ma10:.2f}，量比{volume_ratio:.2f}）",
                sell_price=ma10,
                sell_ratio=1.0
            )

        # 跌破10日线（无论量能，生命线失守）
        if current_price < ma10 * 0.99:
            return SellCondition(
                should_sell=True,
                reason=f"跌破MA10生命线（{current_price:.2f} < {ma10:.2f}）",
                sell_price=ma10,
                sell_ratio=1.0
            )

        # --- 止盈保护 ---
        if profit_pct >= 15 and volume_ratio > 1.5 and pct_change < 1.0:
            return SellCondition(
                should_sell=True,
                reason=f"止盈保护：盈利{profit_pct:.1f}%且放量滞涨（量比{volume_ratio:.2f}），止盈半仓",
                sell_ratio=0.5
            )

        # --- 第一卖点：减仓50% ---
        # 放量跌破5日线
        if current_price < ma5 * 0.998 and volume_ratio > 1.5:
            return SellCondition(
                should_sell=True,
                reason=f"放量跌破MA5（价{current_price:.2f}<{ma5:.2f}，量比{volume_ratio:.2f}），减仓50%",
                sell_ratio=0.5
            )

        # 高位长阴吞没
        if pct_change <= -5:
            return SellCondition(
                should_sell=True,
                reason=f"高位长阴吞没（跌幅{pct_change:.1f}%），减仓50%",
                sell_ratio=0.5
            )

        # 从阶段高点回撤≥5%
        if position_high is not None and position_high > cost_price * 1.05:
            drawdown = (position_high - current_price) / position_high * 100
            if drawdown >= 5:
                return SellCondition(
                    should_sell=True,
                    reason=f"从高点回撤{drawdown:.1f}%≥5%（高点{position_high:.2f}→现{current_price:.2f}），减仓50%",
                    sell_ratio=0.5
                )

        return SellCondition(should_sell=False, reason="持仓中，趋势未破坏")

    def calculate_position_size(self,
                              available_funds: float,
                              current_price: float) -> int:
        """计算买入数量"""
        max_invest = available_funds * self.MAX_SINGLE_POSITION
        max_shares = int(max_invest / current_price / 100) * 100

        if max_shares < 100:
            logger.warning(f"资金不足，最小买入单位为100股")
            return 0

        return max_shares

    def generate_daily_plan(self) -> str:
        """生成次日交易计划文本"""
        candidates = self.get_buy_candidates()

        if not candidates:
            return "📋 次日交易计划\n\n暂无符合条件的买入信号"

        lines = ["📋 次日交易计划", "=" * 40]
        lines.append("")
        lines.append("> 定位：趋势波段系统。只做主线中的强趋势股，只在分歧回踩时介入。")

        for sig in candidates:
            code = sig['code']
            name = sig['name']
            ma5 = sig.get('ma5', 0)
            ma10 = sig.get('ma10', 0)

            lines.append(f"\n🎯 {name}({code}) 评分:{sig['score']}")
            lines.append(f"   第一次分歧回踩: MA5={ma5:.2f}, MA10={ma10:.2f}")
            lines.append(f"   买入区间: {ma10:.2f} ~ {ma5:.2f}（靠近MA5为佳）")
            lines.append(f"   确认条件: ①不破5日线 ②缩量（量比≤1.0）③涨跌幅±3%内")
            lines.append(f"   最佳时间: 9:40-10:00（激进）或 14:30-15:00（稳健）")
            lines.append(f"   止损位: MA10={ma10:.2f}（收盘跌破清仓）")

        lines.append(f"\n⚠️ 放弃条件: 大盘跌超2% | 个股利空 | 持仓≥{self.MAX_HOLDINGS}只")
        lines.append("⚠️ 不做: 加速追高、情绪高潮接力、连续大阳后追涨")

        return "\n".join(lines)


class SimulatedPortfolio:
    """
    mx-moni投资组合管理器封装
    封装trading.portfolio_manager.PortfolioManager，提供统一的接口
    """

    def __init__(self, api_key: Optional[str] = None):
        from trading.portfolio_manager import PortfolioManager, Position, Order
        self._pm = PortfolioManager(api_key=api_key)
        self._positions_cache: List[Dict] = []
        self._orders_cache: List[Dict] = []

    def get_positions(self) -> List[Dict]:
        """获取当前持仓"""
        try:
            positions = self._pm.get_positions()
            self._positions_cache = [
                {
                    'code': p.code,
                    'name': p.name,
                    'volume': p.volume,
                    'cost_price': p.cost_price,
                    'current_price': p.current_price,
                }
                for p in positions
            ]
        except Exception as e:
            logger.warning(f"获取持仓失败: {e}, 使用缓存")
        return self._positions_cache

    def get_funds(self) -> Dict:
        """获取资金信息"""
        try:
            funds = self._pm.get_funds()
            return {
                'available': funds.get('available', 0.0),
                'total': funds.get('total', 0.0),
            }
        except Exception as e:
            logger.warning(f"获取资金失败: {e}")
            return {'available': 0.0, 'total': 0.0}

    def get_orders(self) -> List[Dict]:
        """获取当日委托"""
        try:
            orders = self._pm.get_orders()
            self._orders_cache = [
                {
                    'order_id': o.order_id,
                    'code': o.code,
                    'name': o.name,
                    'direction': o.direction,
                    'price': o.price,
                    'volume': o.volume,
                    'status': o.status,
                }
                for o in orders
            ]
        except Exception as e:
            logger.warning(f"获取委托失败: {e}, 使用缓存")
        return self._orders_cache

    def buy(self, code: str, name: str, price: float, volume: int) -> Dict:
        """买入股票"""
        try:
            result = self._pm.buy(code, price, volume)
            if result.get('success'):
                logger.info(f"✅ 买入成功: {name}({code}) {volume}股 @{price}")
            else:
                logger.warning(f"❌ 买入失败: {name}({code}) - {result.get('error')}")
            return result
        except Exception as e:
            logger.error(f"买入异常: {e}")
            return {'success': False, 'error': str(e)}

    def sell(self, code: str, price: float, volume: int) -> Dict:
        """卖出股票"""
        try:
            result = self._pm.sell(code, price, volume)
            if result.get('success'):
                logger.info(f"✅ 卖出成功: {code} {volume}股 @{price}")
            else:
                logger.warning(f"❌ 卖出失败: {code} - {result.get('error')}")
            return result
        except Exception as e:
            logger.error(f"卖出异常: {e}")
            return {'success': False, 'error': str(e)}

    def cancel_all_orders(self) -> Dict:
        """撤销所有委托"""
        try:
            result = self._pm.cancel_all_orders()
            logger.info(f"一键撤单完成")
            return result
        except Exception as e:
            logger.error(f"撤单异常: {e}")
            return {'success': False, 'error': str(e)}


class TradingExecutor:
    """
    模拟交易执行器

    职责：
    1. 盘后分析技术分析信号，生成次日交易计划
    2. 盘中监控市场，执行买入/卖出
    """

    def __init__(self,
                 portfolio: Optional[SimulatedPortfolio] = None,
                 plan_file: str = 'trading_plan.json'):
        self.decision_helper = TradeDecisionHelper()
        self.portfolio = portfolio or SimulatedPortfolio()
        self.plan_file = plan_file
        self.trading_plan: Optional[Dict] = None

    def analyze_after_market(self, signals: List[TechnicalSignal]) -> str:
        """
        盘后分析 - 生成次日交易计划
        """
        logger.info("开始盘后分析...")

        self.decision_helper.load_signals(signals)

        positions = self.portfolio.get_positions()
        funds = self.portfolio.get_funds()

        plan = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'candidates': self.decision_helper.get_buy_candidates(),
            'holdings': [{'code': p['code'], 'name': p['name'], 'volume': p['volume']} for p in positions],
            'funds': funds,
            'created_at': datetime.now().isoformat()
        }

        self._save_plan(plan)
        self.trading_plan = plan

        report = self.decision_helper.generate_daily_plan()
        logger.info(f"盘后分析完成，{len(plan['candidates'])} 个买入候选")
        return report

    def execute_buy(self,
                   code: str,
                   name: str,
                   current_price: float,
                   volume_ratio: float,
                   market_status: Dict) -> Optional[Dict]:
        """执行买入检查并下单"""
        now = datetime.now()

        can_buy, time_msg = self.decision_helper.check_time_window(now)
        if not can_buy:
            logger.info(f"{code} {time_msg}")
            return None

        market_change = market_status.get('index_change', 0)
        holdings_count = len(self.portfolio.get_positions())

        can_trade, risk_msg = self.decision_helper.check_risk_conditions(
            market_change,
            has_bad_news=False,
            holdings_count=holdings_count
        )
        if not can_trade:
            logger.info(f"{code} 风险检查未通过: {risk_msg}")
            return None

        signal = None
        for s in self.trading_plan.get('candidates', []):
            if s['code'] == code:
                signal = s
                break

        if not signal:
            logger.warning(f"{code} 不在交易计划中")
            return None

        condition = self.decision_helper.check_buy_conditions(
            signal, current_price, volume_ratio, now, market_status
        )

        if not condition.passed:
            logger.info(f"{code} 买入条件未通过: {condition.reason}")
            return None

        if condition.buy_zone:
            lower, upper = condition.buy_zone
            if not (lower <= current_price <= upper):
                logger.info(f"{code} 当前价{current_price}不在买入区间[{lower:.2f}, {upper:.2f}]")
                return None

        funds = self.portfolio.get_funds()
        available = funds.get('available', 0)
        volume = self.decision_helper.calculate_position_size(available, current_price)

        if volume < 100:
            logger.warning(f"{code} 资金不足以买入")
            return None

        result = self.portfolio.buy(code, name, current_price, volume)
        return result

    def check_and_sell(self, position_code: str,
                      current_price: float,
                      current_ma5: float,
                      current_ma10: float,
                      volume_ratio: float = 1.0,
                      pct_change: float = 0.0,
                      position_high: Optional[float] = None) -> Optional[Dict]:
        """检查持仓卖出条件并执行"""
        positions = self.portfolio.get_positions()
        position = None
        for p in positions:
            if p['code'] == position_code:
                position = p
                break

        if not position:
            return None

        sell_condition = self.decision_helper.check_sell_conditions(
            position, current_price, current_ma10, current_ma5,
            position['cost_price'],
            volume_ratio=volume_ratio,
            pct_change=pct_change,
            position_high=position_high or position.get('high_price'),
        )

        if not sell_condition.should_sell:
            return None

        sell_volume = int(position['volume'] * sell_condition.sell_ratio / 100) * 100
        if sell_volume < 100:
            sell_volume = position['volume']

        logger.info(f"{position_code} 执行卖出: {sell_volume}股 - {sell_condition.reason}")

        result = self.portfolio.sell(position_code, current_price, sell_volume)
        return result

    def cancel_pending_orders(self):
        """撤销所有未成交委托"""
        self.portfolio.cancel_all_orders()

    def _save_plan(self, plan: Dict):
        """保存交易计划到文件"""
        with open(self.plan_file, 'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        logger.info(f"交易计划已保存: {self.plan_file}")

    def load_plan(self) -> Optional[Dict]:
        """从文件加载交易计划"""
        if os.path.exists(self.plan_file):
            with open(self.plan_file, 'r', encoding='utf-8') as f:
                self.trading_plan = json.load(f)
            return self.trading_plan
        return None


def _get_realtime_data(code: str) -> Optional[Dict]:
    """获取单只股票实时行情"""
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        row = df[df['代码'] == code]

        if row.empty:
            for suffix in ['SH', 'SZ', 'BJ']:
                full_code = f"{code}.{suffix}"
                row = df[df['代码'] == code]
                if not row.empty:
                    break

        if row.empty:
            logger.warning(f"获取 {code} 实时行情失败")
            return None

        row = row.iloc[0]

        ma5, ma10 = None, None
        try:
            hist = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'),
                end_date=datetime.now().strftime('%Y%m%d')
            )
            if hist is not None and len(hist) >= 10:
                ma5 = hist['收盘'].iloc[-5:].mean()
                ma10 = hist['收盘'].iloc[-10:].mean()
        except Exception as e:
            logger.debug(f"获取 {code} 历史数据失败: {e}")

        change_pct = float(row.get('涨跌幅', 0))

        return {
            'price': float(row.get('最新价', 0)),
            'volume_ratio': float(row.get('量比', 1.0)),
            'ma5': ma5,
            'ma10': ma10,
            'change_pct': change_pct,
        }

    except Exception as e:
        logger.error(f"获取 {code} 实时数据异常: {e}")
        return None


def show_trade_plan(plan_file: str = 'trading_plan.json'):
    """显示当前交易计划"""
    if not os.path.exists(plan_file):
        print("暂无交易计划，请先运行 --trade")
        return

    with open(plan_file, 'r', encoding='utf-8') as f:
        plan = json.load(f)

    print(f"\n{'='*50}")
    print(f"📅 交易日期: {plan['date']}")
    print(f"{'='*50}")

    candidates = plan.get('candidates', [])
    holdings = plan.get('holdings', [])
    funds = plan.get('funds', {})

    if candidates:
        print(f"\n🎯 买入候选 ({len(candidates)} 只):")
        for s in candidates:
            ma5 = s.get('ma5', '-')
            ma10 = s.get('ma10', '-')
            print(f"   {s['name']}({s['code']}) 评分:{s['score']} MA5:{ma5} MA10:{ma10}")
    else:
        print("\n🎯 买入候选: 无")

    if holdings:
        print(f"\n💼 当前持仓 ({len(holdings)} 只):")
        for h in holdings:
            print(f"   {h['name']}({h['code']}) {h['volume']}股")
    else:
        print("\n💼 当前持仓: 空仓")

    print(f"\n💰 可用资金: {funds.get('available', '未知')}")


def run_trade_analysis():
    """盘后分析模式"""
    analyzer = SimpleTechnicalAnalyzer()

    stock_codes, name_mapping = analyzer.mx_service.fetch_self_selected()
    if not stock_codes:
        return "今日无符合条件的信号"

    stock_list = list(zip(stock_codes, [name_mapping.get(c, c) for c in stock_codes]))
    signals, _ = analyzer.analyze_all_stocks(stock_list)

    if not signals:
        return "今日无符合条件的信号"

    executor = TradingExecutor()
    report = executor.analyze_after_market(signals)

    return report


def run_trade_execute():
    """盘中执行模式"""
    now = datetime.now()
    t = now.time()

    if not (time(9, 25) <= t <= time(15, 0)):
        logger.info(f"非交易时间({t.strftime('%H:%M')}), 跳过")
        return "非交易时间，跳过执行"

    executor = TradingExecutor()
    plan = executor.load_plan()

    if not plan:
        logger.warning("无交易计划")
        return "无交易计划，请先运行 --trade"

    logger.info(f"加载交易计划: {plan.get('date')}")
    results = []

    positions = executor.portfolio.get_positions()
    if positions:
        logger.info(f"开始检查 {len(positions)} 个持仓的止损止盈...")
        for pos in positions:
            rt = _get_realtime_data(pos['code'])

            if rt is None or rt['ma10'] is None:
                logger.warning(f"{pos['name']}({pos['code']}) 获取实时数据失败，跳过卖出检查")
                continue

            sell_result = executor.check_and_sell(
                position_code=pos['code'],
                current_price=rt['price'],
                current_ma5=rt['ma5'] if rt['ma5'] else rt['price'],
                current_ma10=rt['ma10'],
                volume_ratio=rt['volume_ratio'] if rt.get('volume_ratio') else 1.0,
                pct_change=rt.get('change_pct', 0.0),
            )

            if sell_result:
                action = f"🔴 卖出 {pos['name']}({pos['code']}): {sell_result}"
                logger.info(action)
                results.append(action)
            else:
                logger.debug(f"{pos['name']}({pos['code']}) 持仓正常，无需操作")
    else:
        logger.info("当前无持仓")
        results.append("💼 当前空仓，无卖出操作")

    candidates = plan.get('candidates', [])
    if not candidates:
        logger.info("无买入候选")
        results.append("🎯 今日无买入候选")
    else:
        logger.info(f"开始检查 {len(candidates)} 个买入候选...")
        market_status = {
            'index_change': 0,
            'market_open': True,
            'is_near_close': t >= time(14, 30),
        }

        for candidate in candidates:
            code = candidate['code']
            name = candidate.get('name', code)

            rt = _get_realtime_data(code)

            if rt is None:
                logger.warning(f"{name}({code}) 获取实时数据失败，跳过")
                continue

            buy_result = executor.execute_buy(
                code=code,
                name=name,
                current_price=rt['price'],
                volume_ratio=rt['volume_ratio'],
                market_status=market_status
            )

            if buy_result:
                action = f"🟢 买入 {name}({code}): {buy_result}"
                logger.info(action)
                results.append(action)
            else:
                logger.info(f"{name}({code}) 未满足买入条件，跳过")

    if t >= time(14, 50):
        try:
            executor.cancel_pending_orders()
            results.append("🧹 收盘前委托已清理")
        except Exception as e:
            logger.warning(f"清理委托失败: {e}")

    return "\n".join(results) if results else "✅ 所有持仓正常，无买卖操作"


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='简化版趋势跟踪系统 - 无 LLM（集成模拟交易）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试模式'
    )

    parser.add_argument(
        '--no-notify',
        action='store_true',
        help='不发送推送通知'
    )

    parser.add_argument(
        '--stocks',
        type=str,
        help='指定要分析的股票代码，逗号分隔（覆盖妙想自选股）'
    )

    trade_group = parser.add_argument_group('交易模式（可选）')
    trade_group.add_argument(
        '--trade',
        action='store_true',
        help='盘后分析模式：分析技术信号并生成次日交易计划'
    )
    trade_group.add_argument(
        '--trade-execute',
        action='store_true',
        help='盘中执行模式：检查持仓止损止盈，执行买入'
    )
    trade_group.add_argument(
        '--trade-plan',
        action='store_true',
        help='查看当前交易计划'
    )

    return parser.parse_args()


def main():
    """主入口"""
    args = parse_arguments()
    
    # 配置日志
    from src.logging_config import setup_logging
    setup_logging(log_prefix="stock_analysis_simple", debug=args.debug)
    
    logger.info("=" * 60)
    logger.info("简化版趋势跟踪系统启动 (无 LLM)")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    try:
        analyzer = SimpleTechnicalAnalyzer()
        
        # 1. 获取股票列表（从妙想或命令行）
        if args.stocks:
            # 使用命令行指定的股票
            stock_codes = [canonical_stock_code(c) for c in args.stocks.split(',') if c.strip()]
            name_mapping = {code: code for code in stock_codes}
            logger.info(f"使用指定股票列表: {stock_codes}")
        else:
            # 从妙想获取
            stock_codes, name_mapping = analyzer.mx_service.fetch_self_selected()
        
        if not stock_codes:
            logger.error("没有获取到股票列表，退出")
            return 1
        
        # 2. 技术分析（包含剔除检查）
        stock_list = list(zip(stock_codes, [name_mapping.get(c, c) for c in stock_codes]))
        logger.info(f"当前关注列表: {len(stock_list)} 只股票")

        signals, removed_stocks = analyzer.analyze_all_stocks(stock_list)

        # 3. 从妙想删除剔除的股票（仅当不是命令行指定模式时）
        if removed_stocks and not args.stocks:
            removed_codes = [code for code, _, _ in removed_stocks]
            success = analyzer.mx_service.remove_stocks(removed_codes)
            if success:
                logger.info(f"已从妙想删除 {len(removed_codes)} 只自选股")
            else:
                logger.warning("从妙想删除失败")
        
        # 7. 检查市场环境并生成报告
        can_trade, market_conditions, market_summary, market_regime = check_market_gate(
            mx_service=analyzer.mx_service,
        )
        logger.info(market_summary)

        report = analyzer.generate_report(signals, removed_stocks,
                                          market_env=(can_trade, market_conditions, market_summary, market_regime))
        
        # 保存报告
        reports_dir = "reports"
        os.makedirs(reports_dir, exist_ok=True)
        today_str = datetime.now().strftime('%Y%m%d')
        report_path = os.path.join(reports_dir, f"technical_simple_{today_str}.md")
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        logger.info(f"报告已保存: {report_path}")
        
        # 8. 发送通知
        if signals and not args.no_notify:
            notifier = NotificationService()
            if notifier.is_available():
                success = notifier.send(report)
                if success:
                    logger.info("通知发送成功")
                else:
                    logger.warning("通知发送失败")
            else:
                logger.warning("通知服务未配置")
        
        logger.info("运行完成")
        return 0
        
    except Exception as e:
        logger.exception(f"运行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
