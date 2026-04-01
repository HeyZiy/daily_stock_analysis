# -*- coding: utf-8 -*-
"""
===================================
简化版趋势跟踪系统 - 无 LLM 版本
===================================

职责：
1. 读取妙想自选股，同步到本地数据库
2. 纯技术分析（缩量回踩 MA5 等规则）
3. 找出符合条件的股票，发送通知

特点：
- 不调用 LLM，节省 API 额度
- 纯本地计算，速度快
- 规则明确，可回测验证

使用方式：
    python main_simple.py              # 正常运行
    python main_simple.py --debug      # 调试模式
"""
import os
import sys
import argparse
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

import pandas as pd
import numpy as np

from src.config import get_config, setup_env
from src.storage import get_db, StockDaily, WatchList
from src.notification import NotificationService
from src.services.mx_service import MXService
from data_provider.base import DataFetcherManager, canonical_stock_code
from sqlalchemy import select, and_, desc

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
    
    # 续命条件：涨幅在 3%-7% 之间
    RENEW_MIN_PCT = 3.0
    RENEW_MAX_PCT = 7.0
    
    # 最大跟踪交易日天数
    MAX_TRADING_DAYS = 3
    
    def __init__(self):
        self.db = get_db()
        self.fetcher = DataFetcherManager()
        self.mx_service = MXService()
        self._trading_dates_cache = None
    
    def get_trading_dates(self, start_date: date, end_date: date) -> List[date]:
        """
        获取交易日列表（使用 Akshare，免费）
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            交易日列表
        """
        if self._trading_dates_cache is not None:
            # 使用缓存
            return [d for d in self._trading_dates_cache if start_date <= d <= end_date]
        
        try:
            # 使用 Akshare 获取交易日历（免费）
            import akshare as ak
            
            # 获取交易日历
            cal_df = ak.tool_trade_date_hist_sina()
            
            # 转换为 date 列表
            trading_dates = []
            for _, row in cal_df.iterrows():
                trade_date = pd.to_datetime(row['trade_date']).date()
                if start_date <= trade_date <= end_date:
                    trading_dates.append(trade_date)
            
            self._trading_dates_cache = trading_dates
            return trading_dates
            
        except Exception as e:
            logger.warning(f"获取交易日历失败: {e}，使用自然日近似")
            # 降级：排除周末
            trading_dates = []
            current = start_date
            while current <= end_date:
                if current.weekday() < 5:  # 周一到周五
                    trading_dates.append(current)
                current += timedelta(days=1)
            return trading_dates
    
    def sync_stocks_to_db(self, stock_codes: List[str], name_mapping: Dict[str, str]) -> Tuple[int, int, int]:
        """
        将股票同步到本地数据库的 WatchList 表
        
        - 新股票：记录加入时间
        - 已有股票：更新 last_seen_date
        - 续命检查：当日涨幅在 3%-7% 时重置计时器
        - 不在列表中的股票：保持原样（由剔除逻辑处理）
        
        Returns:
            (新增数量, 更新数量, 续命数量)
        """
        today = date.today()
        added_count = 0
        updated_count = 0
        renewed_count = 0
        
        with self.db.session_scope() as session:
            for code in stock_codes:
                name = name_mapping.get(code, code)
                
                # 检查是否已存在
                existing = session.execute(
                    select(WatchList).where(
                        WatchList.code == code
                    )
                ).scalar_one_or_none()
                
                if not existing:
                    # 新股票，创建记录
                    watch_item = WatchList(
                        code=code,
                        name=name,
                        added_date=today,
                        last_seen_date=today,
                        status='active',
                    )
                    session.add(watch_item)
                    logger.debug(f"新增关注股票: {code}")
                    added_count += 1
                else:
                    # 已有股票，更新最后看到时间
                    existing.last_seen_date = today
                    existing.name = name  # 更新名称（可能变化）
                    if existing.status != 'active':
                        existing.status = 'active'  # 如果之前被移除了，重新激活
                    
                    # 检查是否满足续命条件（涨幅 3%-7%）
                    try:
                        df = self.fetch_stock_data(code, days=5)
                        if df is not None and len(df) >= 2:
                            latest = df.iloc[-1]
                            prev = df.iloc[-2]
                            
                            # 计算当日涨幅
                            pct_change = (latest['close'] - prev['close']) / prev['close'] * 100
                            
                            # 续命条件：涨幅在 3%-7% 之间
                            if self.RENEW_MIN_PCT <= pct_change <= self.RENEW_MAX_PCT:
                                existing.renewed_date = today
                                existing.renew_count = (existing.renew_count or 0) + 1
                                renewed_count += 1
                                logger.info(f"🔄 {name}({code}) 续命！涨幅 {pct_change:.2f}%，重置计时器（第{existing.renew_count}次续命）")
                    except Exception as e:
                        logger.debug(f"检查 {code} 续命条件时出错: {e}")
                    
                    updated_count += 1
                    logger.debug(f"更新关注股票: {code}")
        
        if added_count > 0 or updated_count > 0:
            logger.info(f"同步完成 | 新增:{added_count} 更新:{updated_count} 续命:{renewed_count}")
        
        return added_count, updated_count, renewed_count
    
    def get_active_watch_list(self) -> List[WatchList]:
        """
        获取当前活跃的关注列表（用于分析）
        
        Returns:
            活跃的 WatchList 记录列表
        """
        with self.db.get_session() as session:
            results = session.execute(
                select(WatchList).where(
                    WatchList.status == 'active'
                ).order_by(WatchList.added_date)
            ).scalars().all()
            return list(results)
    
    def get_removed_stocks(self) -> List[Tuple[str, str]]:
        """
        获取已被剔除的股票列表
        
        Returns:
            [(code, reason), ...]
        """
        with self.db.get_session() as session:
            results = session.execute(
                select(WatchList).where(
                    WatchList.status == 'removed'
                ).order_by(desc(WatchList.updated_at))
            ).scalars().all()
            return [(r.code, r.remove_reason) for r in results]
    
    def should_remove_stock(self, watch_item: WatchList) -> Tuple[bool, str]:
        """
        检查股票是否应该剔除
        
        剔除规则：
        1. 加入自选超过3个交易日（使用交易日历计算）
        2. 收盘跌破10日线
        
        Args:
            watch_item: WatchList 记录
            
        Returns:
            (是否剔除, 剔除原因)
        """
        code = watch_item.code
        today = date.today()
        
        # 获取有效日期（续命日期或加入日期）
        effective_date = watch_item.get_effective_date()
        
        # 规则1：检查加入自选的交易日天数
        try:
            # 获取从有效日期到今天的交易日列表
            trading_dates = self.get_trading_dates(effective_date, today)
            # 排除有效日期当天，只计算之后的天数
            trading_days = len([d for d in trading_dates if d > effective_date])
            
            if trading_days > self.MAX_TRADING_DAYS:
                return True, f"跟踪超过{self.MAX_TRADING_DAYS}个交易日（已{trading_days}天）"
        except Exception as e:
            logger.debug(f"计算 {code} 交易日天数时出错: {e}")
            # 降级：使用自然日
            natural_days = (today - effective_date).days
            if natural_days > 5:  # 自然日超过5天（约等于3个交易日）
                return True, f"跟踪超过{natural_days}天（自然日）"
        
        # 规则2：收盘跌破10日线
        try:
            df = self.fetch_stock_data(code, days=30)
            if df is not None and len(df) >= 10:
                df = self.calculate_ma(df)
                latest = df.iloc[-1]
                
                close = latest.get('close')
                ma10 = latest.get('ma10')
                
                if close is not None and ma10 is not None and not pd.isna(ma10):
                    if close < ma10:
                        return True, f"收盘跌破10日线（收盘价:{close:.2f} < MA10:{ma10:.2f}）"
        except Exception as e:
            logger.debug(f"检查 {code} 剔除条件时出错: {e}")
        
        return False, ""
    
    def remove_stocks(self, codes_to_remove: List[Tuple[str, str]]):
        """
        从本地数据库剔除股票（逻辑删除）
        
        Args:
            codes_to_remove: [(code, reason), ...]
        """
        if not codes_to_remove:
            return
        
        logger.info(f"开始剔除 {len(codes_to_remove)} 只股票...")
        
        with self.db.session_scope() as session:
            for code, reason in codes_to_remove:
                watch_item = session.execute(
                    select(WatchList).where(
                        and_(
                            WatchList.code == code,
                            WatchList.status == 'active'
                        )
                    )
                ).scalar_one_or_none()
                
                if watch_item:
                    watch_item.status = 'removed'
                    watch_item.remove_reason = reason
                    logger.info(f"❌ 剔除 {watch_item.name}({code}): {reason}")
    
    def fetch_stock_data(self, code: str, days: int = 30) -> Optional[pd.DataFrame]:
        """
        获取股票历史数据
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            DataFrame 或 None
        """
        try:
            # 尝试从数据库获取
            end_date = date.today()
            start_date = end_date - timedelta(days=days)
            
            data = self.db.get_data_range(code, start_date, end_date)
            
            if len(data) < 20:  # 数据不足，尝试从网络获取
                logger.debug(f"{code} 本地数据不足，从网络获取...")
                # 转换日期格式为字符串（某些数据源需要）
                start_str = start_date.strftime('%Y-%m-%d')
                end_str = end_date.strftime('%Y-%m-%d')
                result = self.fetcher.get_daily_data(code, start_str, end_str)
                # 返回的是 (df, source_name) 元组
                if isinstance(result, tuple) and len(result) >= 1:
                    df = result[0]
                else:
                    df = result
                if df is not None and hasattr(df, 'empty') and not df.empty:
                    # 保存到数据库
                    self.db.save_daily_data(df, code, self.fetcher.__class__.__name__)
                    return df
            else:
                # 转换为 DataFrame
                df = pd.DataFrame([d.to_dict() for d in data])
                return df
            
            return None
            
        except Exception as e:
            logger.warning(f"获取 {code} 数据失败: {e}")
            return None
    
    def calculate_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算移动平均线"""
        df = df.copy()
        df = df.sort_values('date')
        
        df['ma5'] = df['close'].rolling(window=5).mean()
        df['ma10'] = df['close'].rolling(window=10).mean()
        df['ma20'] = df['close'].rolling(window=20).mean()
        
        return df
    
    def calculate_volume_ratio(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算量比（当前成交量 / 前5日平均成交量）"""
        df = df.copy()
        df = df.sort_values('date')
        
        df['volume_ma5'] = df['volume'].rolling(window=5).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma5']
        
        return df
    
    def detect_signals(self, code: str, name: str, df: pd.DataFrame) -> List[TechnicalSignal]:
        """
        检测技术信号
        
        目前支持的信号：
        1. 缩量回踩 MA5（最佳买点）
        2. 缩量回踩 MA10（次优买点）
        3. 放量突破（趋势加速）
        
        关键修复：
        - 回踩信号：允许价格触碰/微破MA5，只要守住MA10
        - 添加涨跌幅过滤：拒绝大阴线和大阳线（极品洗盘是小实体）
        """
        signals = []
        
        if df is None or len(df) < 20:
            return signals
        
        # 计算指标
        df = self.calculate_ma(df)
        df = self.calculate_volume_ratio(df)
        
        # 取最新数据
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else None
        
        current_price = latest['close']
        ma5 = latest['ma5']
        ma10 = latest['ma10']
        ma20 = latest['ma20']
        volume_ratio = latest.get('volume_ratio', 1.0)
        
        if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
            return signals
        
        # 计算乖离率
        bias_ma5 = (current_price - ma5) / ma5 * 100 if ma5 > 0 else 0
        
        # 计算当日涨跌幅
        pct_change = 0.0
        if prev is not None and prev['close'] > 0:
            pct_change = (current_price - prev['close']) / prev['close'] * 100
        
        # 判断趋势状态
        # 严格多头排列：价格在所有均线之上
        is_strict_bullish = current_price > ma5 > ma10 > ma20
        
        # 宽松多头排列：均线本身多头排列，价格守住MA10（允许回踩触碰MA5）
        is_loose_bullish = ma5 > ma10 > ma20 and current_price > ma10
        
        # 判断是否为小实体（极品洗盘特征）
        is_small_body = abs(pct_change) < 3.0
        
        # 获取昨天成交量（用于环比缩量判断）
        prev_volume = prev['volume'] if prev is not None else 0
        current_volume = latest['volume']
        
        # 环比缩量：今天成交量比昨天萎缩至少 30%（断崖式缩量）
        is_volume_shrink = (current_volume < prev_volume * 0.7) and (volume_ratio < 0.8)
        
        # 获取换手率（用于放量突破判断）
        turnover = latest.get('turnover_rate', 0)
        
        # 信号1: 缩量回踩 MA5（最佳买点）
        # 条件：宽松多头排列 + 环比缩量（断崖式）+ 贴近 MA5 + 小实体
        if is_loose_bullish and is_volume_shrink and abs(bias_ma5) < 2.0 and is_small_body:
            signals.append(TechnicalSignal(
                code=code,
                name=name,
                signal_type='pullback_ma5',
                score=90,  # 极品信号，提高评分
                current_price=current_price,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                bias_ma5=bias_ma5,
                volume_ratio=volume_ratio,
                turnover_rate=turnover,
                description=f"断崖缩量回踩MA5，量比{volume_ratio:.2f}，环比缩量{(1-current_volume/prev_volume)*100:.0f}%，涨跌{pct_change:+.2f}%"
            ))
        
        # 信号2: 缩量回踩 MA10（次优买点）
        # 条件：宽松多头排列 + 环比缩量 + 价格接近MA10 + 小实体
        elif is_loose_bullish and is_volume_shrink and current_price > ma10 * 0.98 and is_small_body:
            signals.append(TechnicalSignal(
                code=code,
                name=name,
                signal_type='pullback_ma10',
                score=75,
                current_price=current_price,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                bias_ma5=bias_ma5,
                volume_ratio=volume_ratio,
                turnover_rate=turnover,
                description=f"断崖缩量回踩MA10，量比{volume_ratio:.2f}，环比缩量{(1-current_volume/prev_volume)*100:.0f}%"
            ))
        
        # 信号3: 放量突破（趋势加速）
        # 条件：严格多头排列 + 放量 + 换手率>5%（保证活跃度）+ 乖离率适中（不追高）
        elif is_strict_bullish and volume_ratio > 1.5 and turnover > 5.0 and bias_ma5 > 0 and bias_ma5 < 5:
            signals.append(TechnicalSignal(
                code=code,
                name=name,
                signal_type='breakout',
                score=80,
                current_price=current_price,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                bias_ma5=bias_ma5,
                volume_ratio=volume_ratio,
                turnover_rate=turnover,
                description=f"放量突破，量能放大{volume_ratio:.2f}倍，换手率{turnover:.2f}%，涨跌{pct_change:+.2f}%"
            ))
        
        return signals
    
    def analyze_all_stocks(self, watch_list: List[WatchList]) -> Tuple[List[TechnicalSignal], List[Tuple[str, str]]]:
        """
        分析所有活跃的关注股票，返回技术信号列表和剔除列表
        
        流程：
        1. 先检查剔除条件（加入天数、跌破MA10）
        2. 对保留的股票进行技术分析
        
        Args:
            watch_list: 活跃的关注列表
            
        Returns:
            (技术信号列表, 剔除列表)
        """
        all_signals = []
        removed_stocks = []
        
        logger.info(f"开始处理 {len(watch_list)} 只关注股票...")
        
        for i, watch_item in enumerate(watch_list):
            code = watch_item.code
            name = watch_item.name or code
            
            try:
                # Step 1: 检查剔除条件
                should_remove, remove_reason = self.should_remove_stock(watch_item)
                if should_remove:
                    removed_stocks.append((code, remove_reason))
                    logger.info(f"❌ 剔除 {name}({code}): {remove_reason}")
                    continue
                
                # Step 2: 获取数据并进行技术分析
                df = self.fetch_stock_data(code)
                if df is None:
                    continue
                
                # 检测信号
                signals = self.detect_signals(code, name, df)
                all_signals.extend(signals)
                
                if signals:
                    logger.info(f"✅ {name}({code}): 发现 {len(signals)} 个信号")
                
                # 每10只打印进度
                if (i + 1) % 10 == 0:
                    logger.info(f"进度: {i + 1}/{len(watch_list)}")
                
            except Exception as e:
                logger.warning(f"分析 {code} 失败: {e}")
                continue
        
        # 按评分排序
        all_signals.sort(key=lambda x: x.score, reverse=True)
        
        kept_count = len(watch_list) - len(removed_stocks)
        logger.info(f"处理完成 | 保留:{kept_count} 剔除:{len(removed_stocks)} 信号:{len(all_signals)}")
        return all_signals, removed_stocks
    
    def generate_report(self, signals: List[TechnicalSignal], removed_stocks: List[Tuple[str, str]] = None) -> str:
        """
        生成 Markdown 格式的报告
        
        Args:
            signals: 技术信号列表
            removed_stocks: 被剔除的股票列表 [(code, reason), ...]
        """
        removed_stocks = removed_stocks or []
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        lines = [
            f"# 📊 技术分析日报 ({today_str})",
            "",
            f"> 共发现 **{len(signals)}** 个技术信号 | 剔除 **{len(removed_stocks)}** 只股票",
            "",
            "---",
            "",
        ]
        
        # 显示剔除的股票
        if removed_stocks:
            lines.extend([
                "## ❌ 剔除股票",
                "",
                "| 股票 | 剔除原因 |",
                "|------|----------|",
            ])
            for code, reason in removed_stocks[:20]:  # 最多显示20个
                # 尝试从数据库获取名称
                with self.db.get_session() as session:
                    watch_item = session.execute(
                        select(WatchList).where(WatchList.code == code)
                    ).scalar_one_or_none()
                    name = watch_item.name if watch_item else code
                lines.append(f"| {name}({code}) | {reason} |")
            if len(removed_stocks) > 20:
                lines.append(f"| ... | 等共{len(removed_stocks)}只股票 |")
            lines.extend(["", "---", ""])
        
        # 分类展示
        pullback_signals = [s for s in signals if 'pullback' in s.signal_type]
        breakout_signals = [s for s in signals if s.signal_type == 'breakout']
        
        # 缩量回踩（高优先级）
        if pullback_signals:
            lines.extend([
                "## 🎯 缩量回踩买点（高优先级）",
                "",
                "| 股票 | 价格 | MA5 | 乖离率 | 量比 | 评分 | 描述 |",
                "|------|------|-----|--------|------|------|------|",
            ])
            for s in pullback_signals:
                lines.append(
                    f"| {s.name}({s.code}) | {s.current_price:.2f} | {s.ma5:.2f} | "
                    f"{s.bias_ma5:+.2f}% | {s.volume_ratio:.2f} | {s.score} | {s.description} |"
                )
            lines.append("")
        
        # 放量突破
        if breakout_signals:
            lines.extend([
                "## 🚀 放量突破",
                "",
                "| 股票 | 价格 | MA5 | 乖离率 | 量比 | 评分 | 描述 |",
                "|------|------|-----|--------|------|------|------|",
            ])
            for s in breakout_signals:
                lines.append(
                    f"| {s.name}({s.code}) | {s.current_price:.2f} | {s.ma5:.2f} | "
                    f"{s.bias_ma5:+.2f}% | {s.volume_ratio:.2f} | {s.score} | {s.description} |"
                )
            lines.append("")
        
        # 汇总
        lines.extend([
            "---",
            "",
            "## 📈 信号汇总",
            "",
        ])
        
        for s in signals[:10]:  # 只显示前10个
            emoji = "🟢" if s.score >= 80 else "🟡" if s.score >= 60 else "⚪"
            lines.append(f"{emoji} **{s.name}({s.code})**: {s.description} | 评分:{s.score}")
        
        lines.extend([
            "",
            "---",
            "",
            "**检测规则**:",
            "- 缩量回踩MA5: 多头排列 + 量比<0.8 + 乖离率<2%",
            "- 缩量回踩MA10: 多头排列 + 量比<0.8 + 价格接近MA10",
            "- 放量突破: 多头排列 + 量比>1.5 + 乖离率2-5%",
        ])
        
        return "\n".join(lines)


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='简化版趋势跟踪系统 - 无 LLM',
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
        
        # 2. 同步到本地数据库（记录加入时间，检查续命）
        added, updated, renewed = analyzer.sync_stocks_to_db(stock_codes, name_mapping)
        
        # 3. 获取当前活跃的关注列表
        active_watch_list = analyzer.get_active_watch_list()
        logger.info(f"当前活跃关注列表: {len(active_watch_list)} 只股票")
        
        # 4. 技术分析（包含剔除检查）
        signals, removed_stocks = analyzer.analyze_all_stocks(active_watch_list)
        
        # 5. 记录剔除的股票到数据库
        if removed_stocks:
            analyzer.remove_stocks(removed_stocks)
        
        # 6. 从妙想删除剔除的股票（仅当不是命令行指定模式时）
        if removed_stocks and not args.stocks:
            removed_codes = [code for code, _ in removed_stocks]
            success = analyzer.mx_service.remove_stocks(removed_codes)
            if success:
                logger.info(f"已从妙想删除 {len(removed_codes)} 只自选股")
            else:
                logger.warning(f"从妙想删除失败")
        
        # 7. 生成报告
        report = analyzer.generate_report(signals, removed_stocks)
        
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
