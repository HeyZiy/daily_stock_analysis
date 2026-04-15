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
import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple

import pandas as pd
from sqlalchemy import select, and_, desc

from data_provider.base import DataFetcherManager, canonical_stock_code
from src.config import setup_env
from src.notification import NotificationService
from src.services.mx_service import MXService
from src.storage import get_db, WatchList
from src.stock_analyzer import StockTrendAnalyzer

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
                    
                    # 续命检查移到技术分析阶段，避免重复拉取数据
                    
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
    
    def should_remove_stock(self, watch_item: WatchList, df: Optional[pd.DataFrame] = None) -> Tuple[bool, str]:
        """
        检查股票是否应该剔除
        
        剔除规则：
        1. 加入自选超过3个交易日（使用交易日历计算）
        2. 收盘跌破10日线
        
        Args:
            watch_item: WatchList 记录
            df: 可选，已预先拉取的行情数据；传入则跳过内部拉取，避免重复请求
            
        Returns:
            (是否剔除, 剔除原因)
        """
        code = watch_item.code
        today = date.today()
        
        # 获取有效日期（续命日期或加入日期）
        effective_date = watch_item.get_effective_date()
        
        # 规则1：检查加入自选的交易日天数
        # 获取从有效日期到今天的交易日列表
        trading_dates = self.get_trading_dates(effective_date, today)
        if not trading_dates:
            # 无法获取交易日历，跳过此规则（不剔除），但记录警告
            logger.warning(f"⚠️ {code} 无法获取交易日历，跳过天数检查")
        else:
            # 排除有效日期当天，只计算之后的天数
            trading_days = len([d for d in trading_dates if d > effective_date])
            
            if trading_days > self.MAX_TRADING_DAYS:
                return True, f"跟踪超过{self.MAX_TRADING_DAYS}个交易日（已{trading_days}天）"
        
        # 规则2：收盘跌破10日线
        try:
            if df is None:
                df = self.fetch_stock_data(code, days=30)
            if df is not None and len(df) >= 10:
                df = df.sort_values('date').reset_index(drop=True)
                df = self.trend_analyzer._calculate_mas(df)
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
        
        修复：检查数据新鲜度，如果最新数据早于昨天，强制从网络获取
        
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
            
            # 检查数据新鲜度：使用交易日历判断
            need_refresh = True
            latest_date = None
            
            if data:
                latest_date = max(d.date for d in data)
                
                # 使用交易日历获取最近一个交易日（自动处理周末和节假日）
                try:
                    # 获取最近30天的交易日
                    trading_dates = self.get_trading_dates(end_date - timedelta(days=30), end_date)
                    if trading_dates:
                        last_trading_day = trading_dates[-1]  # 最近一个交易日
                        
                        # 如果最新数据 >= 最近一个交易日，说明数据是新鲜的
                        if latest_date >= last_trading_day:
                            need_refresh = False
                            logger.debug(f"{code} 本地数据新鲜(最新:{latest_date})，直接使用")
                        else:
                            logger.debug(f"{code} 本地数据过期(最新:{latest_date}, 需要:{last_trading_day}+)，从网络获取...")
                    else:
                        logger.debug(f"{code} 无法获取交易日历，默认从网络获取...")
                except Exception as e:
                    logger.debug(f"{code} 获取交易日历失败({e})，默认从网络获取...")
            else:
                logger.debug(f"{code} 本地无数据，从网络获取...")
            
            if need_refresh:
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
                    # 验证数据新鲜度：最新数据必须 >= 最近一个交易日
                    df_latest_date = pd.to_datetime(df['date'].max()).date()
                    trading_dates = self.get_trading_dates(end_date - timedelta(days=30), end_date)
                    if trading_dates:
                        last_trading_day = trading_dates[-1]
                        if df_latest_date < last_trading_day:
                            logger.error(f"❌ {code} 网络获取的数据仍过期(最新:{df_latest_date}, 需要:{last_trading_day})，拒绝使用")
                            return None
                    
                    # 保存到数据库
                    self.db.save_daily_data(df, code, self.fetcher.__class__.__name__)
                    return df
                else:
                    logger.error(f"❌ {code} 从网络获取数据失败，拒绝使用本地过期数据")
                    return None
            else:
                # 转换为 DataFrame
                df = pd.DataFrame([d.to_dict() for d in data])
                return df
            
        except Exception as e:
            logger.warning(f"获取 {code} 数据失败: {e}")
            return None
    
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
        # 条件：宽松多头排列 + 环比缩量 + 价格在MA10窄幅区间(-2%~+3%) + 小实体
        # 修复：添加上限防止强势横盘股被误判为回踩MA10
        elif is_loose_bullish and is_volume_shrink and (ma10 * 0.98 < current_price < ma10 * 1.03) and is_small_body:
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
                # Step 1: 获取数据（供剔除检查和技术分析共用，避免重复拉取）
                df = self.fetch_stock_data(code)
                
                # Step 2: 检查剔除条件（传入已拉取的 df，跳过内部重复请求）
                should_remove, remove_reason = self.should_remove_stock(watch_item, df=df)
                if should_remove:
                    removed_stocks.append((code, remove_reason))
                    logger.info(f"❌ 剔除 {name}({code}): {remove_reason}")
                    continue
                
                # Step 3: 获取数据并进行技术分析
                if df is None:
                    continue
                
                # 检查续命条件（涨幅 3%-7%）
                if len(df) >= 2:
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                    
                    # 计算当日涨幅
                    pct_change = (latest['close'] - prev['close']) / prev['close'] * 100
                    
                    # 续命条件：涨幅在 3%-7% 之间
                    if self.RENEW_MIN_PCT <= pct_change <= self.RENEW_MAX_PCT:
                        with self.db.session_scope() as session:
                            # 使用行级锁（FOR UPDATE）避免并发更新冲突
                            updated_item = session.execute(
                                select(WatchList).where(
                                    WatchList.code == code
                                ).with_for_update()  # 添加行级锁
                            ).scalar_one_or_none()
                            if updated_item:
                                today = date.today()
                                updated_item.renewed_date = today
                                updated_item.renew_count = (updated_item.renew_count or 0) + 1
                                logger.info(f"🔄 {name}({code}) 续命！涨幅 {pct_change:.2f}%，重置计时器（第{updated_item.renew_count}次续命）")
                
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
        analyzer.sync_stocks_to_db(stock_codes, name_mapping)
        
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
