# -*- coding: utf-8 -*-
"""
模拟盘组合管理器 - 封装 mx-moni Skill 的交互
"""
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional, List, Dict

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """持仓信息"""
    code: str
    name: str
    volume: int
    cost_price: float
    current_price: float
    market_value: float
    profit_pct: float


@dataclass
class Order:
    """委托信息"""
    order_id: str
    code: str
    name: str
    direction: str  # buy/sell
    price: float
    volume: int
    status: str  # pending/filled/cancelled


class PortfolioManager:
    """
    模拟盘组合管理器
    
    封装 mx-moni.py 脚本的调用，提供简化的 Python API
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv('MX_APIKEY')
        self.api_url = os.getenv('MX_API_URL', 'https://mkapi2.dfcfs.com/finskillshub')
        self.skill_path = self._find_skill_path()
        
    def _find_skill_path(self) -> str:
        """查找 mx-moni skill 脚本路径"""
        # 优先查找用户目录下的 skill
        user_skill = os.path.expanduser('~/.workbuddy/skills/mx-moni/mx_moni.py')
        if os.path.exists(user_skill):
            return user_skill
        
        # 备选：项目目录
        project_skill = os.path.join(os.path.dirname(__file__), '..', 'skills', 'mx-moni', 'mx_moni.py')
        if os.path.exists(project_skill):
            return project_skill
        
        return 'mx_moni.py'  # 假设在 PATH 中
    
    def _call_skill(self, query: str) -> Dict:
        """调用 mx-moni skill"""
        env = os.environ.copy()
        if self.api_key:
            env['MX_APIKEY'] = self.api_key
        env['MX_API_URL'] = self.api_url
        
        cmd = ['python', self.skill_path, query]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Skill 调用失败: {result.stderr}")
                return {'error': result.stderr}
            
            # 解析输出
            output = result.stdout
            logger.debug(f"Skill 输出: {output}")
            
            return self._parse_output(output)
            
        except subprocess.TimeoutExpired:
            logger.error("Skill 调用超时")
            return {'error': 'timeout'}
        except Exception as e:
            logger.error(f"Skill 调用异常: {e}")
            return {'error': str(e)}
    
    def _parse_output(self, output: str) -> Dict:
        """解析 skill 输出"""
        # 尝试提取 JSON 或关键信息
        lines = output.strip().split('\n')
        
        result = {
            'raw_output': output,
            'success': 'error' not in output.lower() and '失败' not in output
        }
        
        # 简单解析持仓、资金等信息
        for line in lines:
            if '可用资金' in line or '总资产' in line:
                result['has_fund_info'] = True
            if '持仓' in line and '股票' in line:
                result['has_position'] = True
            if '委托' in line:
                result['has_order'] = True
        
        return result
    
    def get_positions(self) -> List[Position]:
        """获取当前持仓"""
        result = self._call_skill('我的持仓')
        
        positions = []
        # TODO: 根据实际输出格式解析持仓列表
        # 这里需要根据 mx-moni 的实际返回格式调整
        
        return positions
    
    def get_funds(self) -> Dict:
        """获取资金信息"""
        result = self._call_skill('我的资金')
        
        # 返回可用资金、总资产等
        return {
            'available': 0.0,  # 可用资金
            'total': 0.0,      # 总资产
            'raw': result
        }
    
    def get_orders(self) -> List[Order]:
        """获取当日委托"""
        result = self._call_skill('我的委托')
        
        orders = []
        # TODO: 解析委托列表
        
        return orders
    
    def buy(self, code: str, price: float, volume: int, market_order: bool = False) -> Dict:
        """
        买入股票
        
        Args:
            code: 股票代码
            price: 委托价格（市价单时忽略）
            volume: 数量（100的整数倍）
            market_order: 是否市价单
        """
        if market_order:
            query = f'市价买入 {code} {volume}股'
        else:
            query = f'买入 {code} 价格 {price} 数量 {volume}股'
        
        result = self._call_skill(query)
        
        if result.get('success'):
            logger.info(f"买入委托成功: {code} {volume}股 @ {price}")
        else:
            logger.error(f"买入委托失败: {code} - {result.get('error')}")
        
        return result
    
    def sell(self, code: str, price: float, volume: int, market_order: bool = False) -> Dict:
        """卖出股票"""
        if market_order:
            query = f'市价卖出 {code} {volume}股'
        else:
            query = f'卖出 {code} 价格 {price} 数量 {volume}股'
        
        result = self._call_skill(query)
        
        if result.get('success'):
            logger.info(f"卖出委托成功: {code} {volume}股 @ {price}")
        else:
            logger.error(f"卖出委托失败: {code} - {result.get('error')}")
        
        return result
    
    def cancel_all_orders(self) -> Dict:
        """一键撤单"""
        result = self._call_skill('一键撤单')
        logger.info("执行一键撤单")
        return result
    
    def cancel_order(self, order_id: str) -> Dict:
        """撤销指定委托"""
        result = self._call_skill(f'撤单 {order_id}')
        return result
