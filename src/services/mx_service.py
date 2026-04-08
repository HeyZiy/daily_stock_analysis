# -*- coding: utf-8 -*-
"""
===================================
妙想 API 服务模块
===================================

职责：
1. 封装妙想 API 的所有调用（获取自选股、删除自选股等）
2. 提供批量删除功能
3. 统一错误处理和日志记录

使用方式：
    from src.services.mx_service import MXService
    
    service = MXService()
    
    # 获取自选股
    codes, names = service.fetch_self_selected()
    
    # 批量删除
    success = service.remove_stocks(["002284", "600775", ...])
"""

import logging
from typing import List, Dict, Tuple, Optional

import requests

from src.config import get_config

logger = logging.getLogger(__name__)


class MXService:
    """妙想 API 服务类"""
    
    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw"
    
    def __init__(self):
        """初始化，从配置获取 API Key"""
        config = get_config()
        self.api_key = config.mx_apikey
        
        if not self.api_key:
            logger.warning("未配置 MX_APIKEY，妙想功能不可用")
        
        self.headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key
        }
    
    
    def fetch_self_selected(self) -> Tuple[List[str], Dict[str, str]]:
        """
        获取妙想自选股列表
        
        Returns:
            (股票代码列表, 代码到名称的映射)
        """
        url = f"{self.BASE_URL}/self-select/get"
        
        try:
            response = requests.post(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") != 0:
                logger.warning(f"获取妙想自选股失败: {data.get('message', '未知错误')}")
                return [], {}
            
            result = data.get("data", {}).get("allResults", {}).get("result", {})
            data_list = result.get("dataList", [])
            
            stock_codes = []
            name_mapping = {}
            for item in data_list:
                code = item.get("SECURITY_CODE", "").strip()
                name = item.get("SECURITY_SHORT_NAME", "").strip()
                if code:
                    # 统一代码格式（去掉后缀）
                    code = code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
                    stock_codes.append(code)
                    if name:
                        name_mapping[code] = name
            
            logger.info(f"从妙想获取到 {len(stock_codes)} 只自选股")
            return stock_codes, name_mapping
            
        except Exception as e:
            logger.warning(f"获取妙想自选股失败: {e}")
            return [], {}
    
    def remove_stocks(self, codes_to_remove: List[str]) -> bool:
        """
        从妙想自选股批量删除股票
        
        使用自然语言指令一次性删除多个股票
        
        Args:
            codes_to_remove: 要删除的股票代码列表
            
        Returns:
            是否成功
        """
        if not self.is_available():
            logger.warning("未配置 MX_APIKEY，无法删除妙想自选股")
            return False
        
        if not codes_to_remove:
            return True
        
        url = f"{self.BASE_URL}/self-select/manage"
        
        # 构建自然语言指令：把[代码列表]从自选删除
        query = f"把{codes_to_remove}从自选删除"
        payload = {"query": query}
        
        logger.info(f"从妙想批量删除 {len(codes_to_remove)} 只自选股: {codes_to_remove}")
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") == 0:
                logger.info(f"✅ 已从妙想批量删除 {len(codes_to_remove)} 只自选股")
                return True
            else:
                error_msg = data.get("message", "未知错误")
                logger.warning(f"❌ 从妙想删除失败: {error_msg}")
                return False
                
        except Exception as e:
            logger.warning(f"❌ 从妙想删除时出错: {e}")
            return False
