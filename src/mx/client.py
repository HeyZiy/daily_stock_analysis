# -*- coding: utf-8 -*-
"""
===================================
妙想模拟组合 API 客户端
===================================

封装 mx-moni 的 REST API，提供持仓查询、资金查询、买卖下单能力。

API 文档：docs/mx_skills/mx-moni.md
Base URL: https://mkapi2.dfcfs.com/finskillshub
认证：Header apikey: {MX_APIKEY}

股票代码格式：6 位数字（仅 A 股/ETF），系统自动识别市场号。
委托数量：100 的整数倍。
单价字段 API 返回整数（价格 × 10^dec），需要自行还原。
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

MX_BASE_URL = "https://mkapi2.dfcfs.com/finskillshub"


class MXMoniClient:
    """妙想模拟组合 API 客户端"""

    def __init__(self, api_key: str = None, base_url: str = None):
        self.api_key = api_key or os.environ.get("MX_APIKEY", "")
        self.base_url = (base_url or MX_BASE_URL).rstrip("/")

    def _request(self, endpoint: str, payload: dict) -> Optional[dict]:
        url = f"{self.base_url}{endpoint}"
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json; charset=UTF-8",
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"MX API 请求失败 [{endpoint}]: {e}")
            return None

    # ── 资金查询 ──

    def get_balance(self) -> Optional[dict]:
        """查询账户资金与总资产"""
        result = self._request("/api/claw/mockTrading/balance", {"moneyUnit": 1})
        if result and result.get("code") in ("0", "200"):
            data = result.get("data", {})
            currency_unit = data.get("currencyUnit", 1)
            return {
                "total_assets": (data.get("totalAssets", 0) or 0) / currency_unit,
                "avail_balance": (data.get("availBalance", 0) or 0) / currency_unit,
                "frozen_money": (data.get("frozenMoney", 0) or 0) / currency_unit,
                "total_pos_value": (data.get("totalPosValue", 0) or 0) / currency_unit,
                "total_pos_pct": data.get("totalPosPct", 0) or 0,
            }
        return None

    # ── 持仓查询 ──

    def _raw_price(self, price: int, price_dec: int) -> float:
        if price is None or price_dec is None:
            return 0.0
        if price_dec <= 0:
            return float(price)
        return price / (10 ** price_dec)

    def get_positions(self) -> List[dict]:
        """查询当前持仓，返回标准化列表

        Returns:
            [{code, name, count, avail_count, cost_price, current_price,
              market_value, profit, profit_pct, pos_pct}, ...]
        """
        result = self._request("/api/claw/mockTrading/positions", {"moneyUnit": 1})
        positions: List[dict] = []
        if not result or result.get("code") not in ("0", "200"):
            return positions

        data = result.get("data", {})
        pos_list = data.get("posList") or []
        for p in pos_list:
            positions.append({
                "code": p.get("secCode", ""),
                "name": p.get("secName", ""),
                "count": p.get("count", 0) or 0,
                "avail_count": p.get("availCount", 0) or 0,
                "cost_price": self._raw_price(p.get("costPrice"), p.get("costPriceDec")),
                "current_price": self._raw_price(p.get("price"), p.get("priceDec")),
                "market_value": (p.get("value", 0) or 0),
                "profit": (p.get("profit", 0) or 0),
                "profit_pct": p.get("profitPct", 0) or 0,
                "pos_pct": p.get("posPct", 0) or 0,
            })
        return positions

    # ── 买入卖出 ──

    def trade(self, trade_type: str, stock_code: str, quantity: int,
              price: float = 0.0, use_market_price: bool = True) -> Optional[dict]:
        """执行买入或卖出

        Args:
            trade_type: 'buy' 或 'sell'
            stock_code: 6 位数字股票/ETF 代码
            quantity: 股数（100 的整数倍）
            price: 委托价格（use_market_price=False 时必填）
            use_market_price: 是否以市价成交（默认 True）

        Returns:
            API 响应 dict，含 orderId 等字段
        """
        payload: Dict[str, Any] = {
            "type": trade_type,
            "stockCode": stock_code,
            "quantity": quantity,
            "useMarketPrice": use_market_price,
        }
        if not use_market_price:
            payload["price"] = price

        result = self._request("/api/claw/mockTrading/trade", payload)
        if result and result.get("code") in ("0", "200"):
            logger.info(f"{'买入' if trade_type == 'buy' else '卖出'} {stock_code} {quantity}股 成功")
        else:
            msg = (result or {}).get("message", "未知错误")
            logger.warning(f"{'买入' if trade_type == 'buy' else '卖出'} {stock_code} 失败: {msg}")
        return result

    # ── 委托查询 ──

    def get_orders(self, direction: int = 0, status: int = 0) -> Optional[dict]:
        """查询委托订单"""
        return self._request("/api/claw/mockTrading/orders", {
            "fltOrderDrt": direction,
            "fltOrderStatus": status,
        })

    def get_last_buy_dates(self) -> Dict[str, str]:
        """查询历史成交，返回 {code: 最近一次买入成交日期(YYYY-MM-DD)}。

        用于无状态推导持仓起点：持仓接口无"持仓天数"字段，
        用历史委托（drt=1、已成交）的委托时间替代。
        """
        result = self.get_orders(direction=1)
        dates: Dict[str, str] = {}
        if not result or result.get("code") not in ("0", "200"):
            return dates
        orders = (result.get("data") or {}).get("orders") or []
        for o in orders:
            code = o.get("secCode", "")
            if o.get("status", 0) not in (3, 4):  # 部成/已成
                continue
            ts = o.get("time", 0)
            if not code or not ts:
                continue
            try:
                d = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                continue
            if code not in dates or d > dates[code]:
                dates[code] = d
        return dates

    # ── 撤单 ──

    def cancel_order(self, order_id: str = None, stock_code: str = None) -> Optional[dict]:
        """撤单：指定 order_id 撤单，或一键撤单（order_id=None）"""
        if order_id:
            return self._request("/api/claw/mockTrading/cancel", {
                "type": "order",
                "orderId": order_id,
                "stockCode": stock_code or "",
            })
        return self._request("/api/claw/mockTrading/cancel", {
            "type": "all",
        })
