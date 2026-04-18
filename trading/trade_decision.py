# -*- coding: utf-8 -*-
"""
交易决策助手 - 基于策略手册的买入/卖出决策
"""
import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional, Dict, List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class BuyCondition:
    """买入条件检查结果"""
    passed: bool
    reason: str
    buy_zone: Optional[Tuple[float, float]] = None  # (下限, 上限)


@dataclass  
class SellCondition:
    """卖出条件检查结果"""
    should_sell: bool
    reason: str
    sell_price: Optional[float] = None
    sell_ratio: float = 1.0  # 卖出比例 0-1


class TradeDecisionHelper:
    """
    基于《技术分析日报·次日实战交易手册》的决策辅助
    """
    
    # 交易时间段
    MARKET_OPEN = time(9, 30)
    MORNING_END = time(11, 30)
    AFTERNOON_START = time(13, 0)
    MARKET_CLOSE = time(15, 0)
    
    # 买入时间窗口
    BEST_BUY_START = time(9, 40)
    BEST_BUY_END = time(10, 0)
    SAFE_BUY_START = time(14, 30)
    
    # 风险控制参数
    MAX_SINGLE_POSITION = 0.30  # 单票最大仓位 30%
    MAX_HOLDINGS = 2  # 最大持仓数
    STOP_LOSS_MA10 = True  # MA10止损
    
    def __init__(self):
        self.signals: List[Dict] = []
        
    def load_signals(self, signals_df: pd.DataFrame):
        """加载技术分析信号"""
        self.signals = []
        for _, row in signals_df.iterrows():
            if row.get('signal_type') == 'pullback_ma5' and row.get('score', 0) >= 80:
                self.signals.append({
                    'code': row['code'],
                    'name': row['name'],
                    'score': row['score'],
                    'current_price': row.get('current_price'),
                    'ma5': row.get('ma5'),
                    'ma10': row.get('ma10'),
                    'ma20': row.get('ma20'),
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
        """
        检查3个必须买入条件
        
        条件：
        1. 不跌破昨日MA10（趋势生命线）
        2. 开盘量比≤1.0（延续缩量）
        3. 日内涨跌幅±3%以内
        """
        ma10 = signal.get('ma10')
        ma5 = signal.get('ma5')
        
        # 条件1：不跌破MA10
        if current_price < ma10 * 0.995:  # 留0.5%缓冲
            return BuyCondition(
                passed=False,
                reason=f"跌破MA10生命线(当前{current_price:.2f} < MA10:{ma10:.2f})"
            )
        
        # 条件2：量比≤1.0
        if current_volume_ratio > 1.0:
            return BuyCondition(
                passed=False,
                reason=f"放量({current_volume_ratio:.2f} > 1.0)，非缩量洗盘"
            )
        
        # 条件3：涨跌幅±3%以内
        prev_close = signal.get('current_price', current_price)
        change_pct = (current_price - prev_close) / prev_close * 100
        if abs(change_pct) > 3:
            return BuyCondition(
                passed=False,
                reason=f"涨跌幅过大({change_pct:+.2f}%)，超出±3%范围"
            )
        
        # 计算买入区间：MA10 ~ MA5
        buy_zone = (ma10 * 0.998, ma5 * 1.002) if ma10 and ma5 else None
        
        return BuyCondition(
            passed=True,
            reason="满足所有买入条件",
            buy_zone=buy_zone
        )
    
    def check_time_window(self, current_time: datetime) -> Tuple[bool, str]:
        """检查是否在允许买入的时间窗口"""
        t = current_time.time()
        
        # 9:40-10:00 激进窗口
        if self.BEST_BUY_START <= t <= self.BEST_BUY_END:
            return True, "激进买入窗口(9:40-10:00)"
        
        # 14:30-15:00 稳健窗口
        if self.SAFE_BUY_START <= t <= self.MARKET_CLOSE:
            return True, "稳健买入窗口(14:30-15:00)"
        
        # 禁止集合竞价
        if time(9, 25) <= t < time(9, 30):
            return False, "集合竞价时段，禁止买入"
        
        return False, f"非最佳买入时段({t.strftime('%H:%M')})"
    
    def check_risk_conditions(self,
                            market_index_change: float,
                            has_bad_news: bool,
                            holdings_count: int) -> Tuple[bool, str]:
        """
        检查风险放弃条件
        
        触发任一条件直接放弃买入：
        1. 大盘/创业板大跌超2%
        2. 个股突发利空
        3. 持仓已达上限
        """
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
                            cost_price: float) -> SellCondition:
        """
        检查卖出条件
        
        止损：有效跌破MA10
        止盈：
        - 3-5% 减仓50%
        - 7-10% 清仓
        """
        profit_pct = (current_price - cost_price) / cost_price * 100
        
        # 止损：跌破MA10
        if current_price < ma10 * 0.995:
            return SellCondition(
                should_sell=True,
                reason=f"止损：跌破MA10生命线({current_price:.2f} < {ma10:.2f})",
                sell_price=ma10,
                sell_ratio=1.0
            )
        
        # 第一止盈：3-5%
        if 3 <= profit_pct < 7:
            return SellCondition(
                should_sell=True,
                reason=f"第一止盈：盈利{profit_pct:.2f}%，减仓50%",
                sell_ratio=0.5
            )
        
        # 第二止盈：7-10%
        if profit_pct >= 7:
            return SellCondition(
                should_sell=True,
                reason=f"第二止盈：盈利{profit_pct:.2f}%，清仓",
                sell_ratio=1.0
            )
        
        # 趋势止盈：破MA5减仓
        if current_price < ma5 * 0.998:
            return SellCondition(
                should_sell=True,
                reason=f"趋势走弱：跌破MA5({current_price:.2f} < {ma5:.2f})",
                sell_ratio=0.5
            )
        
        return SellCondition(should_sell=False, reason="持仓中")
    
    def calculate_position_size(self, 
                              available_funds: float,
                              current_price: float) -> int:
        """
        计算买入数量
        
        规则：
        - 单票仓位不超过30%
        - 数量为100的整数倍
        """
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
        
        for sig in candidates:
            code = sig['code']
            name = sig['name']
            ma5 = sig.get('ma5', 0)
            ma10 = sig.get('ma10', 0)
            
            lines.append(f"\n🎯 {name}({code}) 评分:{sig['score']}")
            lines.append(f"   买入区间: {ma10:.2f} ~ {ma5:.2f}")
            lines.append(f"   买入条件: ①不跌破MA10 ②量比≤1.0 ③涨跌幅±3%内")
            lines.append(f"   最佳时间: 9:40-10:00 或 14:30-15:00")
            lines.append(f"   止损位: MA10={ma10:.2f}")
        
        lines.append(f"\n⚠️ 放弃条件: 大盘跌超2% | 个股利空 | 持仓≥{self.MAX_HOLDINGS}只")
        
        return "\n".join(lines)
