# -*- coding: utf-8 -*-
"""
===================================
ETF 分析模块
===================================

功能：
1. 获取ETF数据
2. 技术分析（趋势、动量、波动率）
3. 资产配置建议
4. 生成ETF分析报告
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum

import pandas as pd
import numpy as np

from data_provider.base import DataFetcherManager
from src.etf_config import ETF_POOL, ETFInfo, ETFCategory, get_etf_pool, get_category_etf_map
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult, TrendStatus

logger = logging.getLogger(__name__)


class MomentumStatus(Enum):
    """动量状态"""
    STRONG_UP = "强势上涨"
    UP = "上涨"
    NEUTRAL = "震荡"
    DOWN = "下跌"
    STRONG_DOWN = "强势下跌"


@dataclass
class ETFPerformance:
    """ETF表现数据"""
    code: str
    name: str
    category: str
    
    # 价格数据
    current_price: float = 0.0
    prev_close: float = 0.0
    
    # 涨跌幅
    change_1d: float = 0.0
    change_5d: float = 0.0
    change_20d: float = 0.0
    change_60d: float = 0.0
    
    # 波动率
    volatility_20d: float = 0.0
    
    # 技术分析
    trend_result: Optional[TrendAnalysisResult] = None
    
    # 动量状态
    momentum_status: MomentumStatus = MomentumStatus.NEUTRAL
    
    # 评分
    score: int = 0
    score_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    
    # 建议
    suggestion: str = "观望"
    
    def to_dict(self) -> Dict:
        return {
            'code': self.code,
            'name': self.name,
            'category': self.category,
            'current_price': self.current_price,
            'change_1d': self.change_1d,
            'change_5d': self.change_5d,
            'change_20d': self.change_20d,
            'change_60d': self.change_60d,
            'volatility_20d': self.volatility_20d,
            'momentum_status': self.momentum_status.value,
            'score': self.score,
            'suggestion': self.suggestion,
            'trend_result': self.trend_result.to_dict() if self.trend_result else None
        }


@dataclass
class CategoryPerformance:
    """类别表现"""
    category: ETFCategory
    etfs: List[ETFPerformance]
    avg_change_1d: float = 0.0
    avg_change_5d: float = 0.0
    avg_change_20d: float = 0.0
    avg_score: float = 0.0
    best_etf: Optional[ETFPerformance] = None
    worst_etf: Optional[ETFPerformance] = None


class ETFAnalyzer:
    """
    ETF分析器
    
    核心功能：
    1. 批量获取ETF数据
    2. 技术分析
    3. 动量评分
    4. 资产配置建议
    """
    
    def __init__(self):
        self.fetcher = DataFetcherManager()
        self.trend_analyzer = StockTrendAnalyzer()
    
    def fetch_etf_data(self, etf_code: str, days: int = 60) -> Optional[pd.DataFrame]:
        """获取单个ETF数据"""
        try:
            end_date = date.today()
            start_date = end_date - timedelta(days=days * 2)
            
            result = self.fetcher.get_daily_data(
                etf_code,
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )
            
            if isinstance(result, tuple):
                df = result[0]
            else:
                df = result
            
            if df is not None and not df.empty:
                df = df.sort_values('date').reset_index(drop=True)
                return df
            
            return None
        except Exception as e:
            logger.warning(f"获取 {etf_code} 数据失败: {e}")
            return None
    
    def analyze_single_etf(self, etf: ETFInfo, days: int = 60) -> Optional[ETFPerformance]:
        """分析单个ETF"""
        df = self.fetch_etf_data(etf.code, days)
        if df is None or len(df) < 20:
            logger.warning(f"{etf.name}({etf.code}) 数据不足")
            return None
        
        perf = ETFPerformance(
            code=etf.code,
            name=etf.name,
            category=etf.category.value
        )
        
        # 基础价格数据
        latest = df.iloc[-1]
        perf.current_price = float(latest['close'])
        perf.prev_close = float(df.iloc[-2]['close']) if len(df) >= 2 else perf.current_price
        
        # 计算涨跌幅
        perf.change_1d = self._calc_change(df, 1)
        perf.change_5d = self._calc_change(df, 5)
        perf.change_20d = self._calc_change(df, 20)
        perf.change_60d = self._calc_change(df, 60)
        
        # 计算波动率
        if len(df) >= 20:
            returns = df['close'].pct_change().dropna()
            perf.volatility_20d = float(returns.iloc[-20:].std() * np.sqrt(252) * 100)
        
        # 技术分析
        df_ta = self.trend_analyzer._calculate_mas(df)
        perf.trend_result = self.trend_analyzer.analyze(df_ta, etf.code)
        
        # 动量状态判断
        perf.momentum_status = self._judge_momentum(perf)
        
        # 综合评分
        self._score_etf(perf)
        
        # 建议
        self._generate_suggestion(perf)
        
        return perf
    
    def analyze_all_etfs(self) -> Dict[str, ETFPerformance]:
        """分析所有ETF"""
        results = {}
        
        for etf in ETF_POOL:
            logger.info(f"分析 {etf.name}({etf.code})...")
            perf = self.analyze_single_etf(etf)
            if perf:
                results[etf.code] = perf
        
        return results
    
    def analyze_by_category(self) -> Dict[ETFCategory, CategoryPerformance]:
        """按类别分析"""
        etf_results = self.analyze_all_etfs()
        category_map = get_category_etf_map()
        
        category_results = {}
        
        for category, etfs in category_map.items():
            category_perf = CategoryPerformance(category=category, etfs=[])
            
            for etf in etfs:
                if etf.code in etf_results:
                    category_perf.etfs.append(etf_results[etf.code])
            
            if category_perf.etfs:
                # 计算平均表现
                category_perf.avg_change_1d = np.mean([e.change_1d for e in category_perf.etfs])
                category_perf.avg_change_5d = np.mean([e.change_5d for e in category_perf.etfs])
                category_perf.avg_change_20d = np.mean([e.change_20d for e in category_perf.etfs])
                category_perf.avg_score = np.mean([e.score for e in category_perf.etfs])
                
                # 找出最佳和最差
                category_perf.best_etf = max(category_perf.etfs, key=lambda x: x.score)
                category_perf.worst_etf = min(category_perf.etfs, key=lambda x: x.score)
            
            category_results[category] = category_perf
        
        return category_results
    
    def get_allocation_suggestion(self) -> Dict:
        """
        生成资产配置建议
        
        策略：
        - 根据市场环境调整进攻/防守比例
        - 每个类别选择1-2只表现好的ETF
        - 控制单一类别仓位
        """
        category_results = self.analyze_by_category()
        
        # 评估市场整体环境
        market_score = self._assess_market_environment(category_results)
        
        # 根据市场环境确定配置比例
        allocation = self._calculate_allocation(market_score, category_results)
        
        return {
            'market_score': market_score,
            'allocation': allocation,
            'suggestions': self._generate_allocation_text(allocation)
        }
    
    def _calc_change(self, df: pd.DataFrame, days: int) -> float:
        """计算涨跌幅"""
        if len(df) < days + 1:
            return 0.0
        prev = df.iloc[-days - 1]['close']
        curr = df.iloc[-1]['close']
        if prev <= 0:
            return 0.0
        return (curr - prev) / prev * 100
    
    def _judge_momentum(self, perf: ETFPerformance) -> MomentumStatus:
        """判断动量状态"""
        if perf.change_20d > 10 and perf.change_5d > 3:
            return MomentumStatus.STRONG_UP
        elif perf.change_20d > 5:
            return MomentumStatus.UP
        elif perf.change_20d < -10 and perf.change_5d < -3:
            return MomentumStatus.STRONG_DOWN
        elif perf.change_20d < -5:
            return MomentumStatus.DOWN
        else:
            return MomentumStatus.NEUTRAL
    
    def _score_etf(self, perf: ETFPerformance) -> None:
        """为ETF评分"""
        score = 0
        reasons = []
        risks = []
        
        # 趋势评分（40分）
        if perf.trend_result:
            trend_scores = {
                TrendStatus.STRONG_BULL: 40,
                TrendStatus.BULL: 35,
                TrendStatus.WEAK_BULL: 25,
                TrendStatus.CONSOLIDATION: 20,
                TrendStatus.WEAK_BEAR: 10,
                TrendStatus.BEAR: 5,
                TrendStatus.STRONG_BEAR: 0,
            }
            score += trend_scores.get(perf.trend_result.trend_status, 20)
            
            if perf.trend_result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
                reasons.append(f"✅ {perf.trend_result.trend_status.value}")
            elif perf.trend_result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
                risks.append(f"⚠️ {perf.trend_result.trend_status.value}")
        
        # 动量评分（30分）
        momentum_scores = {
            MomentumStatus.STRONG_UP: 30,
            MomentumStatus.UP: 25,
            MomentumStatus.NEUTRAL: 15,
            MomentumStatus.DOWN: 5,
            MomentumStatus.STRONG_DOWN: 0,
        }
        score += momentum_scores.get(perf.momentum_status, 15)
        
        # 技术指标评分（30分）
        if perf.trend_result:
            score += int(perf.trend_result.signal_score * 0.3)
        
        perf.score = min(100, max(0, score))
        perf.score_reasons = reasons
        perf.risk_factors = risks
    
    def _generate_suggestion(self, perf: ETFPerformance) -> None:
        """生成建议"""
        if perf.score >= 75:
            perf.suggestion = "强烈关注"
        elif perf.score >= 60:
            perf.suggestion = "关注"
        elif perf.score >= 45:
            perf.suggestion = "持有/观望"
        elif perf.score >= 30:
            perf.suggestion = "谨慎"
        else:
            perf.suggestion = "回避"
    
    def _assess_market_environment(self, category_results: Dict[ETFCategory, CategoryPerformance]) -> float:
        """评估市场环境，返回0-100的分数"""
        # 检查A股宽基表现
        a_broad = category_results.get(ETFCategory.A_BROAD)
        growth = category_results.get(ETFCategory.GROWTH)
        defense = category_results.get(ETFCategory.DEFENSE)
        
        score = 50  # 中性基准
        
        if a_broad and a_broad.avg_change_20d > 5:
            score += 20
        elif a_broad and a_broad.avg_change_20d < -5:
            score -= 20
        
        # 检查成长和防守的相对表现
        if growth and defense:
            if growth.avg_change_20d > defense.avg_change_20d + 5:
                score += 10  # 成长强势，市场风险偏好高
            elif defense.avg_change_20d > growth.avg_change_20d + 5:
                score -= 10  # 防守强势，市场避险
        
        return min(100, max(0, score))
    
    def _calculate_allocation(self, market_score: float, category_results: Dict[ETFCategory, CategoryPerformance]) -> Dict:
        """计算资产配置比例"""
        # 根据市场环境确定进攻/防守比例
        if market_score >= 70:
            # 强势市场，偏进攻
            aggressive_ratio = 0.7
            defensive_ratio = 0.3
        elif market_score >= 50:
            # 中性市场，平衡配置
            aggressive_ratio = 0.5
            defensive_ratio = 0.5
        else:
            # 弱势市场，偏防守
            aggressive_ratio = 0.3
            defensive_ratio = 0.7
        
        # 定义进攻和防守类别
        aggressive_categories = [
            ETFCategory.A_BROAD,
            ETFCategory.GROWTH,
            ETFCategory.COMMODITY,
        ]
        defensive_categories = [
            ETFCategory.DIVIDEND,
            ETFCategory.DEFENSE,
        ]
        overseas_categories = [
            ETFCategory.OVERSEAS,
            ETFCategory.HK,
        ]
        
        allocation = {
            'aggressive': {
                'ratio': aggressive_ratio,
                'categories': []
            },
            'defensive': {
                'ratio': defensive_ratio,
                'categories': []
            },
            'overseas': {
                'ratio': 0.1,  # 海外配置作为卫星
                'categories': []
            }
        }
        
        # 为每个进攻类别分配
        aggressive_per_category = aggressive_ratio / len(aggressive_categories) if aggressive_categories else 0
        for cat in aggressive_categories:
            if cat in category_results and category_results[cat].best_etf:
                allocation['aggressive']['categories'].append({
                    'category': cat.value,
                    'ratio': aggressive_per_category,
                    'recommended_etf': category_results[cat].best_etf.to_dict()
                })
        
        # 为每个防守类别分配
        defensive_per_category = defensive_ratio / len(defensive_categories) if defensive_categories else 0
        for cat in defensive_categories:
            if cat in category_results and category_results[cat].best_etf:
                allocation['defensive']['categories'].append({
                    'category': cat.value,
                    'ratio': defensive_per_category,
                    'recommended_etf': category_results[cat].best_etf.to_dict()
                })
        
        # 海外配置
        overseas_per_category = 0.1 / len(overseas_categories) if overseas_categories else 0
        for cat in overseas_categories:
            if cat in category_results and category_results[cat].best_etf:
                allocation['overseas']['categories'].append({
                    'category': cat.value,
                    'ratio': overseas_per_category,
                    'recommended_etf': category_results[cat].best_etf.to_dict()
                })
        
        return allocation
    
    def _generate_allocation_text(self, allocation: Dict) -> List[str]:
        """生成配置建议文本"""
        suggestions = []
        
        suggestions.append(f"【配置策略】")
        suggestions.append(f"进攻: {allocation['aggressive']['ratio']*100:.0f}% | 防守: {allocation['defensive']['ratio']*100:.0f}% | 海外: 10%")
        suggestions.append("")
        
        suggestions.append(f"【进攻配置】")
        for cat in allocation['aggressive']['categories']:
            etf = cat['recommended_etf']
            suggestions.append(f"  {cat['category']} ({cat['ratio']*100:.0f}%): {etf['name']}({etf['code']}) - {etf['suggestion']}")
        
        suggestions.append("")
        suggestions.append(f"【防守配置】")
        for cat in allocation['defensive']['categories']:
            etf = cat['recommended_etf']
            suggestions.append(f"  {cat['category']} ({cat['ratio']*100:.0f}%): {etf['name']}({etf['code']}) - {etf['suggestion']}")
        
        suggestions.append("")
        suggestions.append(f"【海外配置】")
        for cat in allocation['overseas']['categories']:
            etf = cat['recommended_etf']
            suggestions.append(f"  {cat['category']} ({cat['ratio']*100:.0f}%): {etf['name']}({etf['code']}) - {etf['suggestion']}")
        
        return suggestions


def generate_etf_report(analyzer: ETFAnalyzer) -> str:
    """生成ETF分析报告"""
    category_results = analyzer.analyze_by_category()
    allocation = analyzer.get_allocation_suggestion()
    
    report_lines = []
    report_lines.append("# ETF 分析报告")
    report_lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("")
    
    # 市场环境评估
    report_lines.append("## 市场环境评估")
    report_lines.append(f"市场得分: {allocation['market_score']}/100")
    report_lines.append("")
    
    # 资产配置建议
    report_lines.append("## 资产配置建议")
    for line in allocation['suggestions']:
        report_lines.append(line)
    report_lines.append("")
    
    # 各类别表现
    report_lines.append("## 各类别表现")
    for category in ETFCategory:
        if category not in category_results:
            continue
        cat_perf = category_results[category]
        if not cat_perf.etfs:
            continue
        
        report_lines.append(f"### {category.value}")
        report_lines.append(f"- 平均20日涨跌幅: {cat_perf.avg_change_20d:+.2f}%")
        report_lines.append(f"- 平均评分: {cat_perf.avg_score:.1f}")
        
        if cat_perf.best_etf:
            report_lines.append(f"- 最佳: {cat_perf.best_etf.name}({cat_perf.best_etf.code}) 评分{cat_perf.best_etf.score}")
        
        report_lines.append("")
        
        # 该类别下的ETF详情
        for etf_perf in cat_perf.etfs:
            report_lines.append(f"#### {etf_perf.name}({etf_perf.code})")
            report_lines.append(f"- 当前价格: {etf_perf.current_price:.3f}")
            report_lines.append(f"- 涨跌幅: 1日{etf_perf.change_1d:+.2f}% | 5日{etf_perf.change_5d:+.2f}% | 20日{etf_perf.change_20d:+.2f}%")
            report_lines.append(f"- 波动率(20日): {etf_perf.volatility_20d:.1f}%")
            report_lines.append(f"- 趋势: {etf_perf.trend_result.trend_status.value if etf_perf.trend_result else 'N/A'}")
            report_lines.append(f"- 评分: {etf_perf.score} - {etf_perf.suggestion}")
            
            if etf_perf.score_reasons:
                report_lines.append(f"- 理由: {', '.join(etf_perf.score_reasons)}")
            if etf_perf.risk_factors:
                report_lines.append(f"- 风险: {', '.join(etf_perf.risk_factors)}")
            
            report_lines.append("")
    
    return "\n".join(report_lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    logging.basicConfig(level=logging.INFO)
    
    analyzer = ETFAnalyzer()
    
    print("=" * 60)
    print("ETF 分析测试")
    print("=" * 60)
    
    # 测试分析单个ETF
    print("\n测试分析单个ETF...")
    from src.etf_config import get_etf_pool
    etf = get_etf_pool()[0]
    perf = analyzer.analyze_single_etf(etf)
    if perf:
        print(f"{perf.name}({perf.code}):")
        print(f"  价格: {perf.current_price:.3f}")
        print(f"  涨跌幅: 1D{perf.change_1d:+.2f}% 5D{perf.change_5d:+.2f}% 20D{perf.change_20d:+.2f}%")
        print(f"  评分: {perf.score} - {perf.suggestion}")
    
    # 测试生成报告
    print("\n生成完整报告...")
    report = generate_etf_report(analyzer)
    print(report)
