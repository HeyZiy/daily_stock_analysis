# -*- coding: utf-8 -*-
"""
===================================
ETF 再平衡引擎
===================================

职责：
1. 根据 gate 状态 + 中性基准 → 计算当前目标配比
2. 比较 mx-moni 实际持仓 vs 目标 → 生成调仓指令
3. 执行调仓（通过 mx-moni 交易接口）
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.etf.config import (
    AssetAllocation, AssetType, NEUTRAL_BASELINE, PROTECTED_TYPES,
    get_equity_total_weight, get_gate_offset, get_rebalance_threshold,
    MIN_TRADE_DEVIATION, REBALANCE_TOTAL_THRESHOLD,
)
from src.mx.client import MXMoniClient

logger = logging.getLogger(__name__)


@dataclass
class RebalanceOrder:
    code: str
    name: str
    action: str        # "buy" 或 "sell"
    amount: float      # 交易金额（元）
    quantity: int       # 交易股数
    current_pct: float  # 当前占比
    target_pct: float   # 目标占比
    reason: str         # 原因


@dataclass
class AllocationReport:
    date: str
    gate_state: str
    hard_intercept: bool
    gate_offset: float
    total_assets: float
    target_allocations: List[dict] = field(default_factory=list)
    current_positions: List[dict] = field(default_factory=list)
    orders: List[RebalanceOrder] = field(default_factory=list)
    summary: str = ""


class ETFRebalancer:
    """ETF 再平衡引擎"""

    def __init__(self, mx_client: MXMoniClient = None):
        self.mx_client = mx_client or MXMoniClient()
        self.baseline = NEUTRAL_BASELINE

    # ── 计算目标配比 ──

    def calculate_target(self, gate_state: str = "", hard_intercept: bool = False,
                          equity_offset: float = None) -> Dict[str, float]:
        """计算当前目标配比

        Args:
            gate_state: 市场状态（趋势 gate 用），equity_offset 为 None 时生效
            hard_intercept: 硬拦截标志，equity_offset 为 None 时生效
            equity_offset: 直接指定权益偏移量，不为 None 时覆盖 gate 逻辑

        Returns:
            {code: target_weight} 目标权重（0.0 ~ 1.0）
        """
        if equity_offset is None:
            equity_offset = get_gate_offset(gate_state, hard_intercept)
        offset = equity_offset

        equity_total = get_equity_total_weight()
        if equity_total <= 0:
            logger.warning("权益类总权重为 0，无法计算偏移")
            return {a.code: a.neutral_weight for a in self.baseline}

        target: Dict[str, float] = {}
        for asset in self.baseline:
            if asset.asset_type == AssetType.EQUITY:
                # 权益件比例 + 按权重分配偏移
                target[asset.code] = asset.neutral_weight + offset * (asset.neutral_weight / equity_total)
            elif asset.asset_type == AssetType.CASH:
                # 现金件吸收偏移
                target[asset.code] = asset.neutral_weight - offset
            else:
                # 黄金、债券不变
                target[asset.code] = asset.neutral_weight

        return target

    # ── 比较持仓 ──

    def _fetch_etf_price(self, code: str) -> float:
        """获取 ETF 日线收盘价（盘后分析用，数据稳定）"""
        try:
            import akshare as ak
            prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
            df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
            if df is not None and not df.empty:
                latest = df.sort_values('date').iloc[-1]
                return float(latest['close'])
        except Exception as e:
            logger.debug(f"akshare 获取 {code} 价格失败: {e}")
        # 回退：DataFetcherManager 实时行情
        try:
            from data_provider.base import DataFetcherManager
            fm = DataFetcherManager()
            quote = fm.get_realtime_quote(code)
            if quote and hasattr(quote, 'price') and quote.price:
                return float(quote.price)
        except Exception:
            pass
        return 0.0

    def _fill_missing_prices(self, current: Dict[str, dict]):
        """为未持仓的 ETF 补齐行情价格"""
        needed = [
            a.code for a in self.baseline
            if a.code != "CASH"
            and (a.code not in current or (current[a.code].get("current_price", 0) or 0) <= 0)
        ]
        if not needed:
            return
        logger.info(f"获取 {len(needed)} 只 ETF 的行情价格...")
        for code in needed:
            price = self._fetch_etf_price(code)
            if price > 0:
                if code not in current:
                    current[code] = {
                        "market_value": 0.0, "current_pct": 0.0,
                        "count": 0, "name": "", "current_price": price,
                    }
                else:
                    current[code]["current_price"] = price
                logger.debug(f"{code} 价格: {price:.3f}")

    def _build_current_map(self, positions: List[dict], total_assets: float) -> Dict[str, dict]:
        """将持仓列表转为 {code: {market_value, current_pct}}"""
        result = {}
        for p in positions:
            code = p.get("code", "")
            mv = float(p.get("market_value", 0) or 0)
            pct = mv / total_assets if total_assets > 0 else 0.0
            result[code] = {
                "market_value": mv,
                "current_pct": pct,
                "count": p.get("count", 0),
                "current_price": p.get("current_price", 0),
                "name": p.get("name", ""),
            }
        return result

    def compare(self, target: Dict[str, float], positions: List[dict],
                total_assets: float, gate_state: str, hard_intercept: bool) -> Tuple[List[RebalanceOrder], float]:
        """比较目标 vs 实际，生成调仓指令

        Returns:
            (orders, total_deviation) 调仓指令列表 + 总偏离度
        """
        current = self._build_current_map(positions, total_assets)
        # 补齐未持仓 ETF 的行情价格
        self._fill_missing_prices(current)
        threshold = get_rebalance_threshold(gate_state)
        orders: List[RebalanceOrder] = []
        total_deviation = 0.0

        for asset in self.baseline:
            code = asset.code
            if code == "CASH":
                continue
            target_pct = target.get(code, 0.0)
            cur = current.get(code, {"current_pct": 0.0, "market_value": 0.0, "count": 0, "current_price": 0.0, "name": asset.name})
            cur_pct = cur["current_pct"]
            deviation = target_pct - cur_pct
            total_deviation += abs(deviation)

            if abs(deviation) < MIN_TRADE_DEVIATION:
                continue

            amount = deviation * total_assets
            cur_price = cur.get("current_price", 0) or 0

            if deviation > 0:
                # 需要加仓
                quantity = self._round_lot(abs(amount) / cur_price, "buy") if cur_price > 0 else 0
                if quantity >= 100:
                    orders.append(RebalanceOrder(
                        code=code, name=asset.name, action="buy",
                        amount=abs(amount), quantity=quantity,
                        current_pct=cur_pct, target_pct=target_pct,
                        reason=f"低于目标{deviation*100:.1f}%"
                    ))
            else:
                # 需要减仓
                if gate_state in ("trending_down", "chaos") or hard_intercept:
                    if asset.asset_type in PROTECTED_TYPES:
                        continue  # 黄金/债券不减
                cur_count = cur.get("count", 0) or 0
                quantity = self._round_lot(min(abs(int(abs(amount) / cur_price)) if cur_price > 0 else 0, cur_count), "sell")
                if quantity >= 100:
                    orders.append(RebalanceOrder(
                        code=code, name=asset.name, action="sell",
                        amount=abs(amount), quantity=quantity,
                        current_pct=cur_pct, target_pct=target_pct,
                        reason=f"高于目标{abs(deviation)*100:.1f}%"
                    ))

        # 按 volatility_rank 排序（卖出优先高波动，买入优先低波动）
        vol_map = {a.code: a.volatility_rank for a in self.baseline}
        sells = sorted([o for o in orders if o.action == "sell"], key=lambda o: vol_map.get(o.code, 99), reverse=True)
        buys = sorted([o for o in orders if o.action == "buy"], key=lambda o: vol_map.get(o.code, 99))
        orders = sells + buys

        return orders, total_deviation

    # ── 判断是否触发再平衡 ──

    def should_rebalance(self, orders: List[RebalanceOrder], total_deviation: float,
                         gate_state: str) -> Tuple[bool, str]:
        """判断是否应该执行再平衡"""
        if not orders:
            return False, "无偏离，无需再平衡"

        threshold = get_rebalance_threshold(gate_state)
        if total_deviation > REBALANCE_TOTAL_THRESHOLD:
            return True, f"总偏离度{total_deviation*100:.1f}% > 强制阈值{REBALANCE_TOTAL_THRESHOLD*100:.0f}%"

        if any(abs(o.target_pct - o.current_pct) > threshold for o in orders):
            return True, f"存在单类偏离 > {threshold*100:.0f}%"

        return False, f"偏离在阈值{threshold*100:.0f}%以内"

    # ── 执行调仓 ──

    def execute_orders(self, orders: List[RebalanceOrder]) -> List[dict]:
        """通过 mx-moni 执行调仓指令"""
        results = []
        for order in orders:
            result = self.mx_client.trade(
                trade_type=order.action,
                stock_code=order.code,
                quantity=order.quantity,
                use_market_price=True,
            )
            results.append({
                "order": order,
                "success": result is not None and (result.get("code") in ("0", "200")),
                "response": result,
            })
        return results

    # ── 生成报告 ──

    def generate_report(self, target: Dict[str, float], positions: List[dict],
                         total_assets: float, orders: List[RebalanceOrder],
                         total_deviation: float,
                         gate_state: str = "", hard_intercept: bool = False,
                         equity_offset: float = None,
                         pe_percentile: float = None, current_pe: float = None) -> str:
        """生成 Markdown 格式的 ETF 配置报告

        equity_offset/pe_percentile/current_pe 用于 PE 估值 gate 模式；
        gate_state/hard_intercept 用于趋势 gate 模式（向后兼容）。
        """
        from datetime import datetime

        current = self._build_current_map(positions, total_assets)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 选择偏移来源
        if equity_offset is None:
            equity_offset = get_gate_offset(gate_state, hard_intercept)
        offset = equity_offset

        # 头部信息
        if pe_percentile is not None and current_pe is not None:
            level = "极度低估" if pe_percentile < 20 else \
                    "低估" if pe_percentile < 40 else \
                    "合理" if pe_percentile < 60 else \
                    "高估" if pe_percentile < 80 else "极度高估"
            gate_line = f"**PE 分位**: {pe_percentile:.0f}% ({level}) | **当前 PE**: {current_pe:.1f}"
        else:
            gate_line = f"**Gate**: {gate_state}" + (" + 硬拦截" if hard_intercept else "")

        lines = [
            f"# ETF 长期配置日报",
            f"",
            f"**时间**: {now} | {gate_line}",
            f"**总资产**: {total_assets:,.0f} 元",
            f"**权益偏移**: {'+' if offset >= 0 else ''}{offset*100:.0f}% → 现金",
            f"",
            "---",
            "",
            "## 目标 vs 实际",
            "",
            "| 资产 | 目标% | 实际% | 偏离 | 操作 |",
            "|------|-------|-------|------|------|",
        ]

        order_map = {o.code: o for o in orders}
        actual_sum = 0.0
        rows: List[str] = []
        for asset in self.baseline:
            code = asset.code
            tgt = target.get(code, 0.0) * 100
            if code == "CASH":
                cur = max(0, 100.0 - actual_sum)
            else:
                cur = current.get(code, {"current_pct": 0.0}).get("current_pct", 0.0) * 100
                actual_sum += cur
            dev = tgt - cur
            order = order_map.get(code)
            action = ""
            if order:
                action = f"{'🟢买' if order.action == 'buy' else '🔴卖'} {order.quantity}股"
            rows.append(f"| {asset.name} | {tgt:.1f}% | {cur:.1f}% | {dev:+.1f}% | {action} |")
        lines.extend(rows)

        lines.extend(["", "---", ""])

        if orders:
            lines.append("## 建议调仓指令")
            lines.append("")
            for o in orders:
                lines.append(f"- {o.action.upper()} {o.name}({o.code}) {o.quantity}股 ≈ {o.amount:,.0f}元 — {o.reason}")
            lines.append("")

        lines.extend([
            "---",
            "",
            f"*总偏离度: {total_deviation*100:.1f}%*",
        ])

        return "\n".join(lines)

    # ── 工具函数 ──

    @staticmethod
    def _round_lot(qty: float, action: str) -> int:
        lot = int(qty // 100) * 100
        return max(100, lot)
