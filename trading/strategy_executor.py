# -*- coding: utf-8 -*-
"""
策略执行器 - 盘后分析和盘中交易执行
"""
import json
import logging
import os
from datetime import datetime, time
from typing import Optional, List, Dict

import pandas as pd

from .trade_decision import TradeDecisionHelper, BuyCondition, SellCondition
from .portfolio_manager import PortfolioManager

logger = logging.getLogger(__name__)


class StrategyExecutor:
    """
    策略执行器
    
    职责：
    1. 盘后分析技术分析信号，生成次日交易计划
    2. 盘中监控市场，执行买入/卖出
    """
    
    def __init__(self, 
                 api_key: Optional[str] = None,
                 plan_file: str = 'trading_plan.json'):
        self.decision_helper = TradeDecisionHelper()
        self.portfolio = PortfolioManager(api_key=api_key)
        self.plan_file = plan_file
        self.trading_plan: Optional[Dict] = None
        
    def analyze_after_market(self, signals_df: pd.DataFrame) -> str:
        """
        盘后分析 - 生成次日交易计划
        
        Args:
            signals_df: 技术分析信号 DataFrame
            
        Returns:
            交易计划文本
        """
        logger.info("开始盘后分析...")
        
        # 加载信号
        self.decision_helper.load_signals(signals_df)
        
        # 获取当前持仓和资金
        positions = self.portfolio.get_positions()
        funds = self.portfolio.get_funds()
        
        # 生成交易计划
        plan = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'candidates': self.decision_helper.get_buy_candidates(),
            'holdings': [{'code': p.code, 'name': p.name, 'volume': p.volume} for p in positions],
            'funds': funds,
            'created_at': datetime.now().isoformat()
        }
        
        # 保存计划
        self._save_plan(plan)
        self.trading_plan = plan
        
        # 生成文本报告
        report = self.decision_helper.generate_daily_plan()
        
        logger.info(f"盘后分析完成，{len(plan['candidates'])} 个买入候选")
        return report
    
    def execute_buy(self, 
                   code: str,
                   current_price: float,
                   volume_ratio: float,
                   market_status: Dict) -> Optional[Dict]:
        """
        执行买入检查并下单
        
        Args:
            code: 股票代码
            current_price: 当前价格
            volume_ratio: 当前量比
            market_status: 市场状态信息
            
        Returns:
            下单结果，不满足条件返回 None
        """
        now = datetime.now()
        
        # 检查时间窗口
        can_buy, time_msg = self.decision_helper.check_time_window(now)
        if not can_buy:
            logger.info(f"{code} {time_msg}")
            return None
        
        # 检查风险条件
        market_change = market_status.get('index_change', 0)
        holdings_count = len(self.portfolio.get_positions())
        
        can_trade, risk_msg = self.decision_helper.check_risk_conditions(
            market_change, 
            has_bad_news=False,  # TODO: 接入新闻检查
            holdings_count=holdings_count
        )
        if not can_trade:
            logger.info(f"{code} 风险检查未通过: {risk_msg}")
            return None
        
        # 查找交易计划中的信号
        signal = None
        for s in self.trading_plan.get('candidates', []):
            if s['code'] == code:
                signal = s
                break
        
        if not signal:
            logger.warning(f"{code} 不在交易计划中")
            return None
        
        # 检查买入条件
        condition = self.decision_helper.check_buy_conditions(
            signal, current_price, volume_ratio, now, market_status
        )
        
        if not condition.passed:
            logger.info(f"{code} 买入条件未通过: {condition.reason}")
            return None
        
        # 检查买入区间
        if condition.buy_zone:
            lower, upper = condition.buy_zone
            if not (lower <= current_price <= upper):
                logger.info(f"{code} 当前价{current_price}不在买入区间[{lower:.2f}, {upper:.2f}]")
                return None
        
        # 计算买入数量
        funds = self.portfolio.get_funds()
        available = funds.get('available', 0)
        volume = self.decision_helper.calculate_position_size(available, current_price)
        
        if volume < 100:
            logger.warning(f"{code} 资金不足以买入")
            return None
        
        # 执行买入
        logger.info(f"{code} 执行买入: {volume}股 @ {current_price}")
        result = self.portfolio.buy(code, current_price, volume)
        
        return result
    
    def check_and_sell(self, position_code: str,
                      current_price: float,
                      current_ma5: float,
                      current_ma10: float) -> Optional[Dict]:
        """
        检查持仓卖出条件并执行
        
        Args:
            position_code: 持仓股票代码
            current_price: 当前价格
            current_ma5: 当前MA5
            current_ma10: 当前MA10
            
        Returns:
            卖出结果，不卖出返回 None
        """
        # 获取持仓信息
        positions = self.portfolio.get_positions()
        position = None
        for p in positions:
            if p.code == position_code:
                position = p
                break
        
        if not position:
            return None
        
        # 检查卖出条件
        position_dict = {
            'code': position.code,
            'volume': position.volume,
            'cost_price': position.cost_price
        }
        
        sell_condition = self.decision_helper.check_sell_conditions(
            position_dict, current_price, current_ma10, current_ma5, position.cost_price
        )
        
        if not sell_condition.should_sell:
            return None
        
        # 计算卖出数量
        sell_volume = int(position.volume * sell_condition.sell_ratio / 100) * 100
        if sell_volume < 100:
            sell_volume = position.volume  # 全部卖出
        
        logger.info(f"{position_code} 执行卖出: {sell_volume}股 - {sell_condition.reason}")
        
        # 执行卖出
        result = self.portfolio.sell(position_code, current_price, sell_volume)
        return result
    
    def cancel_pending_orders(self):
        """撤销所有未成交委托（收盘前清理）"""
        orders = self.portfolio.get_orders()
        pending = [o for o in orders if o.status == 'pending']
        
        if pending:
            logger.info(f"撤销 {len(pending)} 个未成交委托")
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
