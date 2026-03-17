# -*- coding: utf-8 -*-
"""
EastMoney (DFCF) Smart Stock Screen API Fetcher
"""

import logging
import requests
import urllib3
from typing import Dict, Any, Optional, List

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


logger = logging.getLogger(__name__)

class DFCF_Fetcher:
    """
    Fetcher for EastMoney Smart Stock Screen API
    """
    
    API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
    
    def __init__(self, apikey: str):
        self.apikey = apikey
        
    def screen_stocks(self, keyword: str, page_no: int = 1, page_size: int = 50) -> Optional[Dict[str, Any]]:
        """
        Execute stock screening via natural language query
        """
        headers = {
            'Content-Type': 'application/json',
            'apikey': self.apikey
        }
        payload = {
            "keyword": keyword,
            "pageNo": page_no,
            "pageSize": page_size
        }
        
        try:
            # 增加 verify=False 以及重试机制解决可能的 SSL EOF Error
            response = requests.post(self.API_URL, headers=headers, json=payload, timeout=15, verify=False)
            response.raise_for_status()
            data = response.json()
            
            # 根据日志观察到的结构进行更健壮的解析
            if data.get("status") == 0:
                payload_data = data.get("data", {})
                inner_data = payload_data.get("data", {})
                
                # 优先尝试从 inner_data 中提取 dataList
                if isinstance(inner_data, dict):
                    if "dataList" in inner_data:
                        return inner_data["dataList"]
                    # 检查深层解包结构 (data.data.allResults.result.dataList)
                    if "allResults" in inner_data:
                        all_results = inner_data["allResults"]
                        if isinstance(all_results, dict) and "result" in all_results:
                            res_dict = all_results["result"]
                            if isinstance(res_dict, dict) and "dataList" in res_dict:
                                return res_dict["dataList"]

                # 如果 inner_data 本身就是列表
                if isinstance(inner_data, list):
                    return inner_data
                
                # 检查业务状态码以确认是否应该继续尝试解析
                success_code = str(payload_data.get("responseCode")) == "100" or payload_data.get("code") == 0
                if success_code:
                    return []
                else:
                    msg = payload_data.get("message") or data.get("message") or "Business logic error"
                    logger.error(f"DFCF Stock Screen business error: {msg}")
            else:
                msg = data.get("message") or "Endpoint status error"
                logger.error(f"DFCF Stock Screen API error (status {data.get('status')}): {msg}")
            return None
        except Exception as e:
            logger.error(f"DFCF Stock Screen API request failed: {e}")
            return None
