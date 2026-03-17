# -*- coding: utf-8 -*-
"""
Stock Screen Service
"""

import logging
from typing import List, Optional
from data_provider.dfcf_fetcher import DFCF_Fetcher
from data_provider.base import canonical_stock_code

logger = logging.getLogger(__name__)

class StockScreenService:
    """
    Service to handle natural language stock screening logic
    """
    
    def __init__(self, apikey: str):
        self.fetcher = DFCF_Fetcher(apikey)
        
    def get_screened_codes(self, keyword: str, max_count: int = 50) -> List[str]:
        """
        Screen stocks and return a list of canonical stock codes
        """
        try:
            logger.info(f"正在调用智能选股 API，提示词: '{keyword}'")
            result = self.fetcher.screen_stocks(keyword, page_size=max_count)
            
            if not result:
                logger.warning(f"智能选股 API 响应为空 (提示词: '{keyword}')")
                return []
                
            data_list = result if isinstance(result, list) else []
                
            codes = []
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                # 尝试多个可能的代码字段名
                raw_code = item.get("SECURITY_CODE") or item.get("code") or item.get("symbol")
                if not raw_code:
                    continue
                    
                code = canonical_stock_code(raw_code)
                codes.append(code)
                
            logger.info(f"智能选股完成，找到 {len(codes)} 只股票")
            return codes
        except Exception as e:
            logger.error(f"智能选股服务运行异常: {e}")
            return []
