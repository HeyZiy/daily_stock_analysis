# -*- coding: utf-8 -*-
"""
===================================
趋势跟踪池服务
===================================

职责：
1. 从分析结果中筛选值得跟踪的股票入池
2. 每日评估池内股票，更新状态
3. 根据出池规则移除不再值得跟踪的股票
4. 生成跟踪池日报
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

from sqlalchemy import select, and_, or_, desc, func

from src.storage import DatabaseManager, TrendTrackingPool
from src.analyzer import AnalysisResult

logger = logging.getLogger(__name__)


@dataclass
class TrendPoolConfig:
    """趋势跟踪池配置"""
    # 入池条件
    min_score: int = 60  # 最低评分
    max_bias: float = 5.0  # 最大乖离率(%)
    max_rsi: float = 70.0  # 最大RSI（非超买）
    require_bullish: bool = True  # 要求多头排列

    # 出池条件
    min_score_exit: int = 40  # 评分低于此值出池
    max_track_days: int = 7  # 最大跟踪天数
    max_score_decline_days: int = 3  # 连续评分下降天数

    # 池子限制
    max_pool_size: int = 20  # 最大跟踪数量

    # 买入触发条件
    buy_trigger_bias: float = 2.0  # 乖离率低于此值可买入
    buy_trigger_pullback: bool = True  # 是否等待回踩


class TrendPoolService:
    """
    趋势跟踪池服务

    使用示例：
        service = TrendPoolService()

        # 每日更新（分析完成后调用）
        service.update_pool(analysis_results)

        # 获取明日观察列表
        watchlist = service.get_tomorrow_watchlist()

        # 标记买入
        service.mark_as_bought('600519', buy_price=1800.0)
    """

    def __init__(self, config: Optional[TrendPoolConfig] = None):
        self.config = config or TrendPoolConfig()
        self.db = DatabaseManager.get_instance()

    def _extract_metrics(self, result: AnalysisResult) -> Dict[str, Any]:
        """从分析结果中提取关键指标"""
        metrics = {
            'score': result.sentiment_score or 0,
            'price': result.current_price,
            'advice': result.operation_advice or '观望',
            'trend': result.trend_prediction or '震荡',
        }

        # 从dashboard中提取技术指标
        dashboard = result.dashboard or {}
        data_perspective = dashboard.get('data_perspective', {})

        # 价格位置
        price_pos = data_perspective.get('price_position', {})
        metrics['bias'] = price_pos.get('bias_ma5', 0)
        metrics['ma5'] = price_pos.get('ma5')
        metrics['ma10'] = price_pos.get('ma10')
        metrics['ma20'] = price_pos.get('ma20')

        # 趋势状态
        trend_status = data_perspective.get('trend_status', {})
        metrics['is_bullish'] = trend_status.get('is_bullish', False)
        metrics['trend_score'] = trend_status.get('trend_score', 0)

        # 尝试从RSI获取（如果dashboard中有）
        # 注意：当前schema中没有直接存RSI，可能需要从其他地方获取
        metrics['rsi'] = 50  # 默认值

        return metrics

    def _should_enter_pool(self, result: AnalysisResult) -> Tuple[bool, str]:
        """
        判断是否满足入池条件

        Returns:
            (是否入池, 原因)
        """
        metrics = self._extract_metrics(result)

        # 条件1：评分达标
        if metrics['score'] < self.config.min_score:
            return False, f"评分{metrics['score']}低于{self.config.min_score}"

        # 条件2：乖离率不超标
        if metrics['bias'] > self.config.max_bias:
            return False, f"乖离率{metrics['bias']:.2f}%超过{self.config.max_bias}%"

        # 条件3：多头排列
        if self.config.require_bullish and not metrics['is_bullish']:
            return False, "非多头排列"

        # 条件4：非严重超买（RSI）
        if metrics['rsi'] > self.config.max_rsi:
            return False, f"RSI{metrics['rsi']:.1f}超买"

        # 条件5：操作建议为买入/持有
        if metrics['advice'] not in ['买入', '加仓', '持有']:
            return False, f"操作建议为{metrics['advice']}"

        return True, "符合入池条件"

    def _should_exit_pool(self, pool_item: TrendTrackingPool, metrics: Dict[str, Any]) -> Tuple[bool, str]:
        """
        判断是否满足出池条件

        Returns:
            (是否出池, 原因)
        """
        # 条件1：评分过低
        if metrics['score'] < self.config.min_score_exit:
            return True, f"评分{metrics['score']}低于{self.config.min_score_exit}"

        # 条件2：跟踪天数超限
        if pool_item.track_days >= self.config.max_track_days:
            return True, f"跟踪天数{pool_item.track_days}超过{self.config.max_track_days}天"

        # 条件3：趋势转空
        if not metrics.get('is_bullish', True):
            return True, "趋势转空（跌破均线）"

        # 条件4：严重超买（错过买点）
        if metrics['bias'] > 10:  # 乖离率超过10%说明涨太多了
            return True, f"乖离率{metrics['bias']:.2f}%过高，错过买点"

        return False, "继续跟踪"

    def add_to_pool(self, result: AnalysisResult, entry_date: Optional[date] = None) -> bool:
        """
        将股票加入跟踪池

        Args:
            result: 分析结果
            entry_date: 入池日期（默认今天）

        Returns:
            是否成功入池
        """
        should_enter, reason = self._should_enter_pool(result)
        if not should_enter:
            logger.debug(f"{result.code} 不入池: {reason}")
            return False

        entry_date = entry_date or date.today()
        metrics = self._extract_metrics(result)

        with self.db.session_scope() as session:
            # 检查是否已在池中
            existing = session.execute(
                select(TrendTrackingPool).where(
                    and_(
                        TrendTrackingPool.code == result.code,
                        TrendTrackingPool.status == 'tracking'
                    )
                )
            ).scalar_one_or_none()

            if existing:
                logger.debug(f"{result.code} 已在跟踪池中")
                return False

            # 创建新记录
            pool_item = TrendTrackingPool(
                code=result.code,
                name=result.name,
                entry_date=entry_date,
                entry_score=metrics['score'],
                entry_price=metrics['price'],
                entry_bias=metrics['bias'],
                entry_rsi=metrics['rsi'],
                entry_advice=metrics['advice'],
                status='tracking',
                track_days=0,
                latest_analysis_date=entry_date,
                latest_score=metrics['score'],
                latest_price=metrics['price'],
                latest_bias=metrics['bias'],
                latest_rsi=metrics['rsi'],
                latest_advice=metrics['advice'],
                trend_status='多头' if metrics['is_bullish'] else '震荡',
            )
            session.add(pool_item)

        logger.info(f"✅ {result.code} {result.name} 加入跟踪池 | 评分:{metrics['score']} 乖离率:{metrics['bias']:.2f}%")
        return True

    def update_pool_item(self, code: str, result: AnalysisResult, analysis_date: Optional[date] = None) -> bool:
        """
        更新池内股票的最新评估

        Args:
            code: 股票代码
            result: 最新分析结果
            analysis_date: 分析日期

        Returns:
            是否更新成功
        """
        analysis_date = analysis_date or date.today()
        metrics = self._extract_metrics(result)

        with self.db.session_scope() as session:
            pool_item = session.execute(
                select(TrendTrackingPool).where(
                    and_(
                        TrendTrackingPool.code == code,
                        TrendTrackingPool.status == 'tracking'
                    )
                )
            ).scalar_one_or_none()

            if not pool_item:
                return False

            # 更新跟踪天数
            days_diff = (analysis_date - pool_item.latest_analysis_date).days if pool_item.latest_analysis_date else 1
            pool_item.track_days += max(1, days_diff)

            # 更新最新评估
            pool_item.latest_analysis_date = analysis_date
            pool_item.latest_score = metrics['score']
            pool_item.latest_price = metrics['price']
            pool_item.latest_bias = metrics['bias']
            pool_item.latest_rsi = metrics['rsi']
            pool_item.latest_advice = metrics['advice']
            pool_item.trend_status = '多头' if metrics['is_bullish'] else '空头' if not metrics['is_bullish'] else '震荡'

        logger.debug(f"📊 {code} 跟踪池状态更新 | 评分:{metrics['score']} 乖离率:{metrics['bias']:.2f}% 天数:{pool_item.track_days}")
        return True

    def remove_from_pool(self, code: str, reason: str, exit_date: Optional[date] = None) -> bool:
        """
        将股票移出跟踪池

        Args:
            code: 股票代码
            reason: 出池原因
            exit_date: 出池日期

        Returns:
            是否成功出池
        """
        exit_date = exit_date or date.today()

        with self.db.session_scope() as session:
            pool_item = session.execute(
                select(TrendTrackingPool).where(
                    and_(
                        TrendTrackingPool.code == code,
                        TrendTrackingPool.status == 'tracking'
                    )
                )
            ).scalar_one_or_none()

            if not pool_item:
                return False

            pool_item.status = 'removed'
            pool_item.exit_date = exit_date
            pool_item.exit_reason = reason
            pool_item.exit_price = pool_item.latest_price

        logger.info(f"❌ {code} {pool_item.name} 移出跟踪池 | 原因:{reason} 跟踪{pool_item.track_days}天")
        return True

    def mark_as_bought(self, code: str, buy_price: float, buy_date: Optional[date] = None, trigger: str = "") -> bool:
        """
        标记股票为已买入

        Args:
            code: 股票代码
            buy_price: 买入价格
            buy_date: 买入日期
            trigger: 买入触发条件

        Returns:
            是否成功标记
        """
        buy_date = buy_date or date.today()

        with self.db.session_scope() as session:
            pool_item = session.execute(
                select(TrendTrackingPool).where(
                    and_(
                        TrendTrackingPool.code == code,
                        TrendTrackingPool.status == 'tracking'
                    )
                )
            ).scalar_one_or_none()

            if not pool_item:
                return False

            pool_item.status = 'bought'
            pool_item.buy_date = buy_date
            pool_item.buy_price = buy_price
            pool_item.buy_trigger = trigger

        logger.info(f"💰 {code} {pool_item.name} 标记为已买入 | 价格:{buy_price} 触发:{trigger}")
        return True

    def update_pool(self, results: List[AnalysisResult], analysis_date: Optional[date] = None) -> Dict[str, Any]:
        """
        每日更新跟踪池（主入口）

        流程：
        1. 评估现有池内股票，决定是继续跟踪还是出池
        2. 从新的分析结果中筛选符合入池条件的股票
        3. 控制池子大小，优先保留评分高的

        Args:
            results: 当日所有股票的分析结果
            analysis_date: 分析日期

        Returns:
            更新统计信息
        """
        analysis_date = analysis_date or date.today()
        stats = {
            'new_entries': [],
            'continued': [],
            'removed': [],
            'total_in_pool': 0,
        }

        # Step 1: 获取当前跟踪中的股票
        current_pool = self.get_tracking_stocks()
        current_codes = {item.code for item in current_pool}
        result_dict = {r.code: r for r in results}

        # Step 2: 评估现有池内股票
        for item in current_pool:
            if item.code in result_dict:
                result = result_dict[item.code]
                metrics = self._extract_metrics(result)

                # 检查是否出池
                should_exit, exit_reason = self._should_exit_pool(item, metrics)
                if should_exit:
                    self.remove_from_pool(item.code, exit_reason, analysis_date)
                    stats['removed'].append({
                        'code': item.code,
                        'name': item.name,
                        'reason': exit_reason,
                        'track_days': item.track_days,
                    })
                else:
                    # 继续跟踪，更新状态
                    self.update_pool_item(item.code, result, analysis_date)
                    stats['continued'].append({
                        'code': item.code,
                        'name': item.name,
                        'score': metrics['score'],
                        'bias': metrics['bias'],
                        'track_days': item.track_days + 1,
                    })
            else:
                # 没有分析结果，视为数据缺失，继续跟踪但不更新
                stats['continued'].append({
                    'code': item.code,
                    'name': item.name,
                    'score': item.latest_score,
                    'bias': item.latest_bias,
                    'track_days': item.track_days + 1,
                    'note': '无最新分析数据',
                })

        # Step 3: 从新的分析结果中筛选入池
        for result in results:
            if result.code in current_codes:
                continue  # 已在池中

            if self.add_to_pool(result, analysis_date):
                metrics = self._extract_metrics(result)
                stats['new_entries'].append({
                    'code': result.code,
                    'name': result.name,
                    'score': metrics['score'],
                    'bias': metrics['bias'],
                })

        # Step 4: 控制池子大小（如果超出限制，移除评分最低的）
        self._enforce_pool_size_limit(stats)

        # 更新统计
        stats['total_in_pool'] = len(self.get_tracking_stocks())

        logger.info(f"📈 跟踪池更新完成 | 新增:{len(stats['new_entries'])} 继续:{len(stats['continued'])} 移除:{len(stats['removed'])} 总计:{stats['total_in_pool']}")
        return stats

    def _enforce_pool_size_limit(self, stats: Dict[str, Any]):
        """强制执行池子大小限制"""
        tracking = self.get_tracking_stocks()
        if len(tracking) <= self.config.max_pool_size:
            return

        # 按评分排序，移除低评分的
        tracking_sorted = sorted(tracking, key=lambda x: x.latest_score or 0)
        to_remove = tracking_sorted[:len(tracking) - self.config.max_pool_size]

        for item in to_remove:
            self.remove_from_pool(item.code, f"池子超限（保留评分前{self.config.max_pool_size}）")
            stats['removed'].append({
                'code': item.code,
                'name': item.name,
                'reason': '池子超限',
            })

    def get_tracking_stocks(self) -> List[TrendTrackingPool]:
        """获取当前跟踪中的所有股票"""
        with self.db.get_session() as session:
            results = session.execute(
                select(TrendTrackingPool).where(
                    TrendTrackingPool.status == 'tracking'
                ).order_by(desc(TrendTrackingPool.latest_score))
            ).scalars().all()
            return list(results)

    def get_tomorrow_watchlist(self) -> List[Dict[str, Any]]:
        """
        获取明日观察列表（最值得关注的股票）

        排序规则：
        1. 乖离率 < 2%（最佳买点）优先
        2. 评分高优先
        3. 跟踪天数短优先
        """
        tracking = self.get_tracking_stocks()

        watchlist = []
        for item in tracking:
            # 计算买入优先级
            bias = item.latest_bias or 0
            if bias <= 2.0:
                priority = 'high'  # 高优先级：可买入
            elif bias <= 5.0:
                priority = 'medium'  # 中优先级：观望
            else:
                priority = 'low'  # 低优先级：等待回调

            watchlist.append({
                'code': item.code,
                'name': item.name,
                'score': item.latest_score,
                'bias': item.latest_bias,
                'rsi': item.latest_rsi,
                'track_days': item.track_days,
                'priority': priority,
                'suggestion': self._get_suggestion(item),
            })

        # 排序：优先级 > 评分 > 跟踪天数
        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        watchlist.sort(key=lambda x: (
            priority_order.get(x['priority'], 3),
            -(x['score'] or 0),
            x['track_days']
        ))

        return watchlist

    def _get_suggestion(self, item: TrendTrackingPool) -> str:
        """生成观察建议"""
        bias = item.latest_bias or 0
        if bias <= 2.0:
            return "🟢 明天可买入"
        elif bias <= 5.0:
            return "🟡 等待回踩MA5"
        else:
            return "⚪ 等待深度回调"

    def generate_daily_report(self) -> str:
        """
        生成跟踪池日报

        Returns:
            Markdown格式的报告
        """
        tracking = self.get_tracking_stocks()
        today_str = date.today().strftime('%Y-%m-%d')

        # 分类
        high_priority = []  # 可买入
        medium_priority = []  # 观望
        low_priority = []  # 等待

        for item in tracking:
            bias = item.latest_bias or 0
            if bias <= 2.0:
                high_priority.append(item)
            elif bias <= 5.0:
                medium_priority.append(item)
            else:
                low_priority.append(item)

        # 生成报告
        lines = [
            f"# 📊 趋势跟踪池日报 ({today_str})",
            "",
            f"> 当前跟踪 **{len(tracking)}** 只股票",
            "",
            "---",
            "",
        ]

        # 高优先级（明天可买入）
        if high_priority:
            lines.extend([
                "## 🎯 明天可买入 (高优先级)",
                "",
                "| 股票 | 评分 | 乖离率 | 跟踪天数 | 操作建议 |",
                "|------|------|--------|----------|----------|",
            ])
            for item in high_priority:
                lines.append(
                    f"| {item.name}({item.code}) | {item.latest_score} | "
                    f"{item.latest_bias:.2f}% | {item.track_days}天 | 🟢 明天可买入 |"
                )
            lines.append("")

        # 中优先级（观望）
        if medium_priority:
            lines.extend([
                "## 👀 继续观望 (中优先级)",
                "",
                "| 股票 | 评分 | 乖离率 | 跟踪天数 | 操作建议 |",
                "|------|------|--------|----------|----------|",
            ])
            for item in medium_priority:
                lines.append(
                    f"| {item.name}({item.code}) | {item.latest_score} | "
                    f"{item.latest_bias:.2f}% | {item.track_days}天 | 🟡 等待回踩MA5 |"
                )
            lines.append("")

        # 低优先级（等待回调）
        if low_priority:
            lines.extend([
                "## ⏳ 等待回调 (低优先级)",
                "",
                "| 股票 | 评分 | 乖离率 | 跟踪天数 | 操作建议 |",
                "|------|------|--------|----------|----------|",
            ])
            for item in low_priority:
                lines.append(
                    f"| {item.name}({item.code}) | {item.latest_score} | "
                    f"{item.latest_bias:.2f}% | {item.track_days}天 | ⚪ 等待深度回调 |"
                )
            lines.append("")

        # 如果没有股票
        if not tracking:
            lines.extend([
                "## 📭 跟踪池为空",
                "",
                "当前没有符合趋势交易条件的股票，请等待选股系统发现新的机会。",
                "",
            ])

        lines.extend([
            "---",
            "",
            "**入池条件**: 评分≥60 | 乖离率<5% | 多头排列 | 非超买",
            "",
            "**出池条件**: 评分<40 | 跟踪>7天 | 趋势转空 | 严重超买",
        ])

        return "\n".join(lines)

    def get_pool_statistics(self) -> Dict[str, Any]:
        """获取跟踪池统计信息"""
        with self.db.get_session() as session:
            # 当前跟踪中
            tracking_count = session.execute(
                select(func.count(TrendTrackingPool.id)).where(
                    TrendTrackingPool.status == 'tracking'
                )
            ).scalar() or 0

            # 已买入
            bought_count = session.execute(
                select(func.count(TrendTrackingPool.id)).where(
                    TrendTrackingPool.status == 'bought'
                )
            ).scalar() or 0

            # 已移除
            removed_count = session.execute(
                select(func.count(TrendTrackingPool.id)).where(
                    TrendTrackingPool.status == 'removed'
                )
            ).scalar() or 0

            # 平均跟踪天数
            avg_days = session.execute(
                select(func.avg(TrendTrackingPool.track_days)).where(
                    TrendTrackingPool.status == 'tracking'
                )
            ).scalar() or 0

            return {
                'tracking': tracking_count,
                'bought': bought_count,
                'removed': removed_count,
                'avg_track_days': round(avg_days, 1),
            }
