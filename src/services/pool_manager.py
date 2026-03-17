# -*- coding: utf-8 -*-
"""
Dynamic Pool Manager Service

管理智能选股流动池的生命周期：
1. 添加新股票（或重置已有股票时间）
2. 每日清理（超过3天、或者跌破10日线）
3. 梯队打标（例如：缩量回踩，站稳五日线 -> 梯队1）
"""

import logging
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Tuple
from sqlalchemy import select, and_, or_, update, delete

from src.storage import DatabaseManager, DynamicPool, StockDaily

logger = logging.getLogger(__name__)

class DynamicPoolManager:
    """
    智能选股流动池管理服务
    """
    
    def __init__(self):
        self.db = DatabaseManager.get_instance()
        
    def add_to_pool(self, codes: List[str]) -> None:
        """
        将新选出的股票加入流动池。
        如果股票已在池中，无论是何种状态，都刷新其 added_date 为今天，并标记为 active。
        """
        if not codes:
            return
            
        today = date.today()
        added_count = 0
        updated_count = 0
        
        with self.db.session_scope() as session:
            for code in codes:
                existing = session.execute(
                    select(DynamicPool).where(DynamicPool.code == code)
                ).scalar_one_or_none()
                
                if existing:
                    # 已经在池中（无论是 active 还是 removed），重置其生命周期
                    existing.added_date = today
                    existing.status = 'active'
                    existing.remove_reason = None
                    existing.tier = 0
                    existing.updated_at = datetime.now()
                    updated_count += 1
                else:
                    # 新手加入
                    new_pool_item = DynamicPool(
                        code=code,
                        added_date=today,
                        status='active',
                        tier=0
                    )
                    session.add(new_pool_item)
                    added_count += 1
                    
        logger.info(f"流动池更新完成: 新增 {added_count} 只, 重置周期 {updated_count} 只")
        
    def get_active_pool(self) -> List[str]:
        """
        获取当前流动的 (active) 股票代码列表
        """
        with self.db.get_session() as session:
            results = session.execute(
                select(DynamicPool.code).where(DynamicPool.status == 'active')
            ).scalars().all()
            return list(results)

    def maintain_pool(self, target_date: date = None) -> None:
        """
        每日维护流动池（清理过期或破坏形态的股票）。
        
        清理规则：
        1. 超过3天（今天 - added_date >= 4天） -> expired
        2. 最新收盘价跌破10日线 -> below_ma10
        """
        if not target_date:
            target_date = date.today()
            
        expired_count = 0
        ma10_broken_count = 0
        
        with self.db.session_scope() as session:
            active_stocks = session.execute(
                select(DynamicPool).where(DynamicPool.status == 'active')
            ).scalars().all()
            
            for pool_item in active_stocks:
                # 规则1: 生命检查 (超过3天，即第4天及以上删除)
                days_alive = (target_date - pool_item.added_date).days
                if days_alive >= 4:
                    pool_item.status = 'removed'
                    pool_item.remove_reason = 'expired'
                    pool_item.updated_at = datetime.now()
                    expired_count += 1
                    logger.info(f"流动池清理: {pool_item.code} 存活 {days_alive} 天已超期，移出")
                    continue
                    
                # 规则2: 形态检查 (收盘跌破10日线)
                # 获取该股票最新一天的数据（通常就是今天或最近一个交易日的数据）
                latest_daily = session.execute(
                    select(StockDaily)
                    .where(StockDaily.code == pool_item.code)
                    .order_by(StockDaily.date.desc())
                    .limit(1)
                ).scalar_one_or_none()
                
                if latest_daily and latest_daily.close and latest_daily.ma10:
                    if latest_daily.close < latest_daily.ma10:
                        pool_item.status = 'removed'
                        pool_item.remove_reason = 'below_ma10'
                        pool_item.updated_at = datetime.now()
                        ma10_broken_count += 1
                        logger.info(f"流动池清理: {pool_item.code} 收盘价({latest_daily.close:.2f}) 跌破 10日线({latest_daily.ma10:.2f})，移出")
                        
            if expired_count > 0 or ma10_broken_count > 0:
                logger.info(f"流动池维护完成: 因超期移出 {expired_count} 只, 因跌破MA10移出 {ma10_broken_count} 只")
            
    def evaluate_tiers(self) -> Dict[str, int]:
        """
        对当前 active 的流动池股票进行梯队评估。
        
        梯队1条件 (Tier 1): 缩量回踩，站稳五日线
        - 缩量：今日成交量 < 昨日成交量
        - 回踩：今日最低价 <= MA5 * 1.02 (非常接近MA5)
        - 站稳：今日收盘价 > MA5
        
        返回:
            { "stock_code": tier_level }
        """
        tier_mapping = {}
        
        with self.db.session_scope() as session:
            active_stocks = session.execute(
                select(DynamicPool).where(DynamicPool.status == 'active')
            ).scalars().all()
            
            for pool_item in active_stocks:
                # 默认设为 0（无梯队）
                pool_item.tier = 0
                
                # 获取最近两天的数据来判断缩量和回踩
                recent_dailies = session.execute(
                    select(StockDaily)
                    .where(StockDaily.code == pool_item.code)
                    .order_by(StockDaily.date.desc())
                    .limit(2)
                ).scalars().all()
                
                if len(recent_dailies) >= 2:
                    today_data = recent_dailies[0]
                    yesterday_data = recent_dailies[1]
                    
                    if today_data.volume and yesterday_data.volume and today_data.low and today_data.close and today_data.ma5:
                        
                        is_shrinking_volume = today_data.volume < yesterday_data.volume
                        is_stepping_back = today_data.low <= (today_data.ma5 * 1.02)
                        is_holding_firm = today_data.close > today_data.ma5
                        
                        if is_shrinking_volume and is_stepping_back and is_holding_firm:
                            pool_item.tier = 1
                            tier_mapping[pool_item.code] = 1
                            logger.info(f"流动池打标: {pool_item.code} 命中 梯队1 (缩量回踩，站稳五日线)")
                            
                pool_item.updated_at = datetime.now()
                
        return tier_mapping

    def get_tier_info(self, code: str) -> int:
        """
        查询单只股票当前的梯队级别
        """
        with self.db.get_session() as session:
            pool_item = session.execute(
                select(DynamicPool).where(DynamicPool.code == code)
            ).scalar_one_or_none()
            
            if pool_item and pool_item.status == 'active':
                return pool_item.tier
        return 0
