# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 通知层
===================================

职责：
1. 汇总分析结果生成日报
2. 支持 Markdown 格式输出
3. 多渠道推送（自动识别）：
   - 企业微信 Webhook
   - 飞书 Webhook
   - Telegram Bot
   - 邮件 SMTP
   - Pushover（手机/桌面推送）
"""
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum

from src.config import get_config
from src.analyzer import AnalysisResult
from src.enums import ReportType
from src.utils.data_processing import normalize_model_used
from src.notification_sender.email_sender import EmailSender

# 企业微信图片大小限制（约 2MB）
WECHAT_IMAGE_MAX_BYTES = 2 * 1024 * 1024

logger = logging.getLogger(__name__)


class NotificationChannel(Enum):
    """通知渠道类型"""
    WECHAT = "wechat"      # 企业微信
    FEISHU = "feishu"      # 飞书
    TELEGRAM = "telegram"  # Telegram
    EMAIL = "email"        # 邮件
    PUSHOVER = "pushover"  # Pushover（手机/桌面推送）
    PUSHPLUS = "pushplus"  # PushPlus（国内推送服务）
    SERVERCHAN3 = "serverchan3"  # Server酱3（手机APP推送服务）
    CUSTOM = "custom"      # 自定义 Webhook
    DISCORD = "discord"    # Discord 机器人 (Bot)
    ASTRBOT = "astrbot"
    UNKNOWN = "unknown"    # 未知


class ChannelDetector:
    """
    渠道检测器 - 简化版
    
    根据配置直接判断渠道类型（不再需要 URL 解析）
    """
    
    @staticmethod
    def get_channel_name(channel: NotificationChannel) -> str:
        """获取渠道中文名称"""
        names = {
            NotificationChannel.WECHAT: "企业微信",
            NotificationChannel.FEISHU: "飞书",
            NotificationChannel.TELEGRAM: "Telegram",
            NotificationChannel.EMAIL: "邮件",
            NotificationChannel.PUSHOVER: "Pushover",
            NotificationChannel.PUSHPLUS: "PushPlus",
            NotificationChannel.SERVERCHAN3: "Server酱3",
            NotificationChannel.CUSTOM: "自定义Webhook",
            NotificationChannel.DISCORD: "Discord机器人",
            NotificationChannel.ASTRBOT: "ASTRBOT机器人",
            NotificationChannel.UNKNOWN: "未知渠道",
        }
        return names.get(channel, "未知渠道")


class NotificationService(EmailSender):
    """
    通知服务
    
    职责：
    1. 生成 Markdown 格式的分析日报
    2. 向所有已配置的渠道推送消息（多渠道并发）
    3. 支持本地保存日报
    
    支持的渠道：
    - 企业微信 Webhook
    - 飞书 Webhook
    - Telegram Bot
    - 邮件 SMTP
    - Pushover（手机/桌面推送）
    
    注意：所有已配置的渠道都会收到推送
    """
    
    def __init__(self):
        """
        初始化通知服务
        
        只初始化邮件发送功能
        """
        config = get_config()

        # Markdown 转图片（Issue #289）
        self._markdown_to_image_channels = set(
            getattr(config, 'markdown_to_image_channels', []) or []
        )
        self._markdown_to_image_max_chars = getattr(
            config, 'markdown_to_image_max_chars', 15000
        )

        # 仅分析结果摘要（Issue #262）：true 时只推送汇总，不含个股详情
        self._report_summary_only = getattr(config, 'report_summary_only', False)
        self._history_compare_cache: Dict[Tuple[int, Tuple[Tuple[str, str], ...]], Dict[str, List[Dict[str, Any]]]] = {}

        # 只初始化邮件渠道
        EmailSender.__init__(self, config)

        # 检测所有已配置的渠道
        self._available_channels = self._detect_all_channels()

        if not self._available_channels:
            logger.warning("未配置有效的通知渠道，将不发送推送通知")
        else:
            channel_names = [ChannelDetector.get_channel_name(ch) for ch in self._available_channels]
            logger.info(f"已配置 {len(channel_names)} 个通知渠道：{', '.join(channel_names)}")

    def _normalize_report_type(self, report_type: Any) -> ReportType:
        """Normalize string/enum input into ReportType."""
        if isinstance(report_type, ReportType):
            return report_type
        return ReportType.from_str(report_type)

    def _get_history_compare_context(self, results: List[AnalysisResult]) -> Dict[str, Any]:
        """Fetch and cache history comparison data for markdown rendering."""
        config = get_config()
        history_compare_n = getattr(config, 'report_history_compare_n', 0)
        if history_compare_n <= 0 or not results:
            return {"history_by_code": {}}

        cache_key = (
            history_compare_n,
            tuple(sorted((r.code, getattr(r, 'query_id', '') or '') for r in results)),
        )
        if cache_key in self._history_compare_cache:
            return {"history_by_code": self._history_compare_cache[cache_key]}

        try:
            from src.services.history_comparison_service import get_signal_changes_batch

            exclude_ids = {
                r.code: r.query_id
                for r in results
                if getattr(r, 'query_id', None)
            }
            codes = list(dict.fromkeys(r.code for r in results))
            history_by_code = get_signal_changes_batch(
                codes,
                limit=history_compare_n,
                exclude_query_ids=exclude_ids,
            )
        except Exception as e:
            logger.debug("History comparison skipped: %s", e)
            history_by_code = {}

        self._history_compare_cache[cache_key] = history_by_code
        return {"history_by_code": history_by_code}

    def generate_aggregate_report(
        self,
        results: List[AnalysisResult],
        report_type: Any,
        report_date: Optional[str] = None,
    ) -> str:
        """Generate the aggregate report content used by merge/save/push paths."""
        normalized_type = self._normalize_report_type(report_type)
        if normalized_type == ReportType.BRIEF:
            return self.generate_brief_report(results, report_date=report_date)
        return self.generate_dashboard_report(results, report_date=report_date)

    def _collect_models_used(self, results: List[AnalysisResult]) -> List[str]:
        models: List[str] = []
        for result in results:
            model = normalize_model_used(getattr(result, "model_used", None))
            if model:
                models.append(model)
        return list(dict.fromkeys(models))
    
    def _detect_all_channels(self) -> List[NotificationChannel]:
        """
        检测所有已配置的渠道
        
        Returns:
            已配置的渠道列表
        """
        channels = []
        
        # 邮件
        if self._is_email_configured():
            channels.append(NotificationChannel.EMAIL)
        
        return channels

    def is_available(self) -> bool:
        """检查通知服务是否可用（至少有一个渠道）"""
        return len(self._available_channels) > 0
    
    def get_available_channels(self) -> List[NotificationChannel]:
        """获取所有已配置的渠道"""
        return self._available_channels
    
    def get_channel_names(self) -> str:
        """获取所有已配置渠道的名称"""
        names = [ChannelDetector.get_channel_name(ch) for ch in self._available_channels]
        return ', '.join(names)
        
    def generate_daily_report(
        self,
        results: List[AnalysisResult],
        report_date: Optional[str] = None
    ) -> str:
        """
        生成 Markdown 格式的日报（详细版）

        Args:
            results: 分析结果列表
            report_date: 报告日期（默认今天）

        Returns:
            Markdown 格式的日报内容
        """
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')

        # 标题
        report_lines = [
            f"# 📅 {report_date} 股票智能分析报告",
            "",
            f"> 共分析 **{len(results)}** 只股票 | 报告生成时间：{datetime.now().strftime('%H:%M:%S')}",
            "",
            "---",
            "",
        ]
        
        # 按评分排序（高分在前）
        sorted_results = sorted(
            results, 
            key=lambda x: x.sentiment_score, 
            reverse=True
        )
        
        # 统计信息 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        avg_score = sum(r.sentiment_score for r in results) / len(results) if results else 0
        
        report_lines.extend([
            "## 📊 操作建议汇总",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 🟢 建议买入/加仓 | **{buy_count}** 只 |",
            f"| 🟡 建议持有/观望 | **{hold_count}** 只 |",
            f"| 🔴 建议减仓/卖出 | **{sell_count}** 只 |",
            f"| 📈 平均看多评分 | **{avg_score:.1f}** 分 |",
            "",
            "---",
            "",
        ])
        
        # Issue #262: summary_only 时仅输出摘要，跳过个股详情
        if self._report_summary_only:
            report_lines.extend(["## 📊 分析结果摘要", ""])
            for r in sorted_results:
                emoji = r.get_emoji()
                report_lines.append(
                    f"{emoji} **{r.name}({r.code})**: {r.operation_advice} | "
                    f"评分 {r.sentiment_score} | {r.trend_prediction}"
                )
        else:
            report_lines.extend(["## 📈 个股详细分析", ""])
            # 逐个股票的详细分析
            for result in sorted_results:
                emoji = result.get_emoji()
                confidence_stars = result.get_confidence_stars() if hasattr(result, 'get_confidence_stars') else '⭐⭐'
                
                report_lines.extend([
                    f"### {emoji} {result.name} ({result.code})",
                    "",
                    f"**操作建议：{result.operation_advice}** | **综合评分：{result.sentiment_score}分** | **趋势预测：{result.trend_prediction}** | **置信度：{confidence_stars}**",
                    "",
                ])

                self._append_market_snapshot(report_lines, result)
                
                # 核心看点
                if hasattr(result, 'key_points') and result.key_points:
                    report_lines.extend([
                        f"**🎯 核心看点**：{result.key_points}",
                        "",
                    ])
                
                # 买入/卖出理由
                if hasattr(result, 'buy_reason') and result.buy_reason:
                    report_lines.extend([
                        f"**💡 操作理由**：{result.buy_reason}",
                        "",
                    ])
                
                # 走势分析
                if hasattr(result, 'trend_analysis') and result.trend_analysis:
                    report_lines.extend([
                        "#### 📉 走势分析",
                        f"{result.trend_analysis}",
                        "",
                    ])
                
                # 短期/中期展望
                outlook_lines = []
                if hasattr(result, 'short_term_outlook') and result.short_term_outlook:
                    outlook_lines.append(f"- **短期（1-3日）**：{result.short_term_outlook}")
                if hasattr(result, 'medium_term_outlook') and result.medium_term_outlook:
                    outlook_lines.append(f"- **中期（1-2周）**：{result.medium_term_outlook}")
                if outlook_lines:
                    report_lines.extend([
                        "#### 🔮 市场展望",
                        *outlook_lines,
                        "",
                    ])
                
                # 技术面分析
                tech_lines = []
                if result.technical_analysis:
                    tech_lines.append(f"**综合**：{result.technical_analysis}")
                if hasattr(result, 'ma_analysis') and result.ma_analysis:
                    tech_lines.append(f"**均线**：{result.ma_analysis}")
                if hasattr(result, 'volume_analysis') and result.volume_analysis:
                    tech_lines.append(f"**量能**：{result.volume_analysis}")
                if hasattr(result, 'pattern_analysis') and result.pattern_analysis:
                    tech_lines.append(f"**形态**：{result.pattern_analysis}")
                if tech_lines:
                    report_lines.extend([
                        "#### 📊 技术面分析",
                        *tech_lines,
                        "",
                    ])
                
                # 基本面分析
                fund_lines = []
                if hasattr(result, 'fundamental_analysis') and result.fundamental_analysis:
                    fund_lines.append(result.fundamental_analysis)
                if hasattr(result, 'sector_position') and result.sector_position:
                    fund_lines.append(f"**板块地位**：{result.sector_position}")
                if hasattr(result, 'company_highlights') and result.company_highlights:
                    fund_lines.append(f"**公司亮点**：{result.company_highlights}")
                if fund_lines:
                    report_lines.extend([
                        "#### 🏢 基本面分析",
                        *fund_lines,
                        "",
                    ])
                
                # 消息面/情绪面
                news_lines = []
                if result.news_summary:
                    news_lines.append(f"**新闻摘要**：{result.news_summary}")
                if hasattr(result, 'market_sentiment') and result.market_sentiment:
                    news_lines.append(f"**市场情绪**：{result.market_sentiment}")
                if hasattr(result, 'hot_topics') and result.hot_topics:
                    news_lines.append(f"**相关热点**：{result.hot_topics}")
                if news_lines:
                    report_lines.extend([
                        "#### 📰 消息面/情绪面",
                        *news_lines,
                        "",
                    ])
                
                # 综合分析
                if result.analysis_summary:
                    report_lines.extend([
                        "#### 📝 综合分析",
                        result.analysis_summary,
                        "",
                    ])
                
                # 风险提示
                if hasattr(result, 'risk_warning') and result.risk_warning:
                    report_lines.extend([
                        f"⚠️ **风险提示**：{result.risk_warning}",
                        "",
                    ])
                
                # 数据来源说明
                if hasattr(result, 'search_performed') and result.search_performed:
                    report_lines.append("*🔍 已执行联网搜索*")
                if hasattr(result, 'data_sources') and result.data_sources:
                    report_lines.append(f"*📋 数据来源：{result.data_sources}*")
                
                # 错误信息（如果有）
                if not result.success and result.error_message:
                    report_lines.extend([
                        "",
                        f"❌ **分析异常**：{result.error_message[:100]}",
                    ])
                
                report_lines.extend([
                    "",
                    "---",
                    "",
                ])
        
        # 底部信息（去除免责声明）
        report_lines.extend([
            "",
            f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        ])
        
        return "\n".join(report_lines)
    
    @staticmethod
    def _escape_md(name: str) -> str:
        """Escape markdown special characters in stock names (e.g. *ST → \\*ST)."""
        return name.replace('*', r'\*') if name else name

    @staticmethod
    def _clean_sniper_value(value: Any) -> str:
        """Normalize sniper point values and remove redundant label prefixes."""
        if value is None:
            return 'N/A'
        if isinstance(value, (int, float)):
            return str(value)
        if not isinstance(value, str):
            return str(value)
        if not value or value == 'N/A':
            return value
        prefixes = ['理想买入点：', '次优买入点：', '止损位：', '目标位：',
                     '理想买入点:', '次优买入点:', '止损位:', '目标位:']
        for prefix in prefixes:
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    def _get_signal_level(self, result: AnalysisResult) -> tuple:
        """
        Get signal level and color based on operation advice.

        Priority: advice string takes precedence over score.
        Score-based fallback is used only when advice doesn't match
        any known value.

        Returns:
            (signal_text, emoji, color_tag)
        """
        advice = result.operation_advice
        score = result.sentiment_score

        # Advice-first lookup (exact match takes priority)
        advice_map = {
            '强烈买入': ('强烈买入', '💚', '强买'),
            '买入': ('买入', '🟢', '买入'),
            '加仓': ('买入', '🟢', '买入'),
            '持有': ('持有', '🟡', '持有'),
            '观望': ('观望', '⚪', '观望'),
            '减仓': ('减仓', '🟠', '减仓'),
            '卖出': ('卖出', '🔴', '卖出'),
            '强烈卖出': ('卖出', '🔴', '卖出'),
        }
        if advice in advice_map:
            return advice_map[advice]

        # Score-based fallback when advice is unrecognized
        if score >= 80:
            return ('强烈买入', '💚', '强买')
        elif score >= 65:
            return ('买入', '🟢', '买入')
        elif score >= 55:
            return ('持有', '🟡', '持有')
        elif score >= 45:
            return ('观望', '⚪', '观望')
        elif score >= 35:
            return ('减仓', '🟠', '减仓')
        elif score < 35:
            return ('卖出', '🔴', '卖出')
        else:
            return ('观望', '⚪', '观望')
    
    def generate_dashboard_report(
        self,
        results: List[AnalysisResult],
        report_date: Optional[str] = None
    ) -> str:
        """
        生成决策仪表盘格式的日报（详细版）

        格式：市场概览 + 重要信息 + 核心结论 + 数据透视 + 作战计划

        Args:
            results: 分析结果列表
            report_date: 报告日期（默认今天）

        Returns:
            Markdown 格式的决策仪表盘日报
        """
        config = get_config()
        if getattr(config, 'report_renderer_enabled', False) and results:
            from src.services.report_renderer import render
            out = render(
                platform='markdown',
                results=results,
                report_date=report_date,
                summary_only=self._report_summary_only,
                extra_context=self._get_history_compare_context(results),
            )
            if out:
                return out

        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')

        # 按评分排序（高分在前）
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)

        # 统计信息 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))

        report_lines = [
            f"# 🎯 {report_date} 决策仪表盘",
            "",
            f"> 共分析 **{len(results)}** 只股票 | 🟢买入:{buy_count} 🟡观望:{hold_count} 🔴卖出:{sell_count}",
            "",
        ]

        # === 新增：分析结果摘要 (Issue #112) ===
        if results:
            report_lines.extend([
                "## 📊 分析结果摘要",
                "",
            ])
            for r in sorted_results:
                _, signal_emoji, _ = self._get_signal_level(r)
                display_name = self._escape_md(r.name)
                report_lines.append(
                    f"{signal_emoji} **{display_name}({r.code})**: {r.operation_advice} | "
                    f"评分 {r.sentiment_score} | {r.trend_prediction}"
                )
            report_lines.extend([
                "",
                "---",
                "",
            ])

        # 逐个股票的决策仪表盘（Issue #262: summary_only 时跳过详情）
        if not self._report_summary_only:
            for result in sorted_results:
                signal_text, signal_emoji, signal_tag = self._get_signal_level(result)
                dashboard = result.dashboard if hasattr(result, 'dashboard') and result.dashboard else {}
                
                # 股票名称（优先使用 dashboard 或 result 中的名称，转义 *ST 等特殊字符）
                raw_name = result.name if result.name and not result.name.startswith('股票') else f'股票{result.code}'
                stock_name = self._escape_md(raw_name)
                
                report_lines.extend([
                    f"## {signal_emoji} {stock_name} ({result.code})",
                    "",
                ])
                
                # ========== 舆情与基本面概览（放在最前面）==========
                intel = dashboard.get('intelligence', {}) if dashboard else {}
                if intel:
                    report_lines.extend([
                        "### 📰 重要信息速览",
                        "",
                    ])
                    # 舆情情绪总结
                    if intel.get('sentiment_summary'):
                        report_lines.append(f"**💭 舆情情绪**: {intel['sentiment_summary']}")
                    # 业绩预期
                    if intel.get('earnings_outlook'):
                        report_lines.append(f"**📊 业绩预期**: {intel['earnings_outlook']}")
                    # 风险警报（醒目显示）
                    risk_alerts = intel.get('risk_alerts', [])
                    if risk_alerts:
                        report_lines.append("")
                        report_lines.append("**🚨 风险警报**:")
                        for alert in risk_alerts:
                            report_lines.append(f"- {alert}")
                    # 利好催化
                    catalysts = intel.get('positive_catalysts', [])
                    if catalysts:
                        report_lines.append("")
                        report_lines.append("**✨ 利好催化**:")
                        for cat in catalysts:
                            report_lines.append(f"- {cat}")
                    # 最新消息
                    if intel.get('latest_news'):
                        report_lines.append("")
                        report_lines.append(f"**📢 最新动态**: {intel['latest_news']}")
                    report_lines.append("")
                
                # ========== 核心结论 ==========
                core = dashboard.get('core_conclusion', {}) if dashboard else {}
                one_sentence = core.get('one_sentence', result.analysis_summary)
                time_sense = core.get('time_sensitivity', '本周内')
                pos_advice = core.get('position_advice', {})
                
                report_lines.extend([
                    "### 📌 核心结论",
                    "",
                    f"**{signal_emoji} {signal_text}** | {result.trend_prediction}",
                    "",
                    f"> **一句话决策**: {one_sentence}",
                    "",
                    f"⏰ **时效性**: {time_sense}",
                    "",
                ])
                # 持仓分类建议
                if pos_advice:
                    report_lines.extend([
                        "| 持仓情况 | 操作建议 |",
                        "|---------|---------|",
                        f"| 🆕 **空仓者** | {pos_advice.get('no_position', result.operation_advice)} |",
                        f"| 💼 **持仓者** | {pos_advice.get('has_position', '继续持有')} |",
                        "",
                    ])

                self._append_market_snapshot(report_lines, result)
                
                # ========== 数据透视 ==========
                data_persp = dashboard.get('data_perspective', {}) if dashboard else {}
                if data_persp:
                    trend_data = data_persp.get('trend_status', {})
                    price_data = data_persp.get('price_position', {})
                    vol_data = data_persp.get('volume_analysis', {})
                    chip_data = data_persp.get('chip_structure', {})
                    
                    report_lines.extend([
                        "### 📊 数据透视",
                        "",
                    ])
                    # 趋势状态
                    if trend_data:
                        is_bullish = "✅ 是" if trend_data.get('is_bullish', False) else "❌ 否"
                        report_lines.extend([
                            f"**均线排列**: {trend_data.get('ma_alignment', 'N/A')} | 多头排列: {is_bullish} | 趋势强度: {trend_data.get('trend_score', 'N/A')}/100",
                            "",
                        ])
                    # 价格位置
                    if price_data:
                        bias_status = price_data.get('bias_status', 'N/A')
                        bias_emoji = "✅" if bias_status == "安全" else ("⚠️" if bias_status == "警戒" else "🚨")
                        report_lines.extend([
                            "| 价格指标 | 数值 |",
                            "|---------|------|",
                            f"| 当前价 | {price_data.get('current_price', 'N/A')} |",
                            f"| MA5 | {price_data.get('ma5', 'N/A')} |",
                            f"| MA10 | {price_data.get('ma10', 'N/A')} |",
                            f"| MA20 | {price_data.get('ma20', 'N/A')} |",
                            f"| 乖离率(MA5) | {price_data.get('bias_ma5', 'N/A')}% {bias_emoji}{bias_status} |",
                            f"| 支撑位 | {price_data.get('support_level', 'N/A')} |",
                            f"| 压力位 | {price_data.get('resistance_level', 'N/A')} |",
                            "",
                        ])
                    # 量能分析
                    if vol_data:
                        report_lines.extend([
                            f"**量能**: 量比 {vol_data.get('volume_ratio', 'N/A')} ({vol_data.get('volume_status', '')}) | 换手率 {vol_data.get('turnover_rate', 'N/A')}%",
                            f"💡 *{vol_data.get('volume_meaning', '')}*",
                            "",
                        ])
                    # 筹码结构
                    if chip_data:
                        chip_health = chip_data.get('chip_health', 'N/A')
                        chip_emoji = "✅" if chip_health == "健康" else ("⚠️" if chip_health == "一般" else "🚨")
                        report_lines.extend([
                            f"**筹码**: 获利比例 {chip_data.get('profit_ratio', 'N/A')} | 平均成本 {chip_data.get('avg_cost', 'N/A')} | 集中度 {chip_data.get('concentration', 'N/A')} {chip_emoji}{chip_health}",
                            "",
                        ])
                
                # ========== 作战计划 ==========
                battle = dashboard.get('battle_plan', {}) if dashboard else {}
                if battle:
                    report_lines.extend([
                        "### 🎯 作战计划",
                        "",
                    ])
                    # 狙击点位
                    sniper = battle.get('sniper_points', {})
                    if sniper:
                        report_lines.extend([
                            "**📍 狙击点位**",
                            "",
                            "| 点位类型 | 价格 |",
                            "|---------|------|",
                            f"| 🎯 理想买入点 | {self._clean_sniper_value(sniper.get('ideal_buy', 'N/A'))} |",
                            f"| 🔵 次优买入点 | {self._clean_sniper_value(sniper.get('secondary_buy', 'N/A'))} |",
                            f"| 🛑 止损位 | {self._clean_sniper_value(sniper.get('stop_loss', 'N/A'))} |",
                            f"| 🎊 目标位 | {self._clean_sniper_value(sniper.get('take_profit', 'N/A'))} |",
                            "",
                        ])
                    # 仓位策略
                    position = battle.get('position_strategy', {})
                    if position:
                        report_lines.extend([
                            f"**💰 仓位建议**: {position.get('suggested_position', 'N/A')}",
                            f"- 建仓策略: {position.get('entry_plan', 'N/A')}",
                            f"- 风控策略: {position.get('risk_control', 'N/A')}",
                            "",
                        ])
                    # 检查清单
                    checklist = battle.get('action_checklist', []) if battle else []
                    if checklist:
                        report_lines.extend([
                            "**✅ 检查清单**",
                            "",
                        ])
                        for item in checklist:
                            report_lines.append(f"- {item}")
                        report_lines.append("")
                
                # 如果没有 dashboard，显示传统格式
                if not dashboard:
                    # 操作理由
                    if result.buy_reason:
                        report_lines.extend([
                            f"**💡 操作理由**: {result.buy_reason}",
                            "",
                        ])
                    # 风险提示
                    if result.risk_warning:
                        report_lines.extend([
                            f"**⚠️ 风险提示**: {result.risk_warning}",
                            "",
                        ])
                    # 技术面分析
                    if result.ma_analysis or result.volume_analysis:
                        report_lines.extend([
                            "### 📊 技术面",
                            "",
                        ])
                        if result.ma_analysis:
                            report_lines.append(f"**均线**: {result.ma_analysis}")
                        if result.volume_analysis:
                            report_lines.append(f"**量能**: {result.volume_analysis}")
                        report_lines.append("")
                    # 消息面
                    if result.news_summary:
                        report_lines.extend([
                            "### 📰 消息面",
                            f"{result.news_summary}",
                            "",
                        ])
                
                report_lines.extend([
                    "---",
                    "",
                ])
        
        # 底部（去除免责声明）
        report_lines.extend([
            "",
            f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        ])
        
        return "\n".join(report_lines)
    
    def generate_wechat_dashboard(self, results: List[AnalysisResult]) -> str:
        """
        生成企业微信决策仪表盘精简版（控制在4000字符内）
        
        只保留核心结论和狙击点位
        
        Args:
            results: 分析结果列表
            
        Returns:
            精简版决策仪表盘
        """
        config = get_config()
        if getattr(config, 'report_renderer_enabled', False) and results:
            from src.services.report_renderer import render
            out = render(
                platform='wechat',
                results=results,
                report_date=datetime.now().strftime('%Y-%m-%d'),
                summary_only=self._report_summary_only,
            )
            if out:
                return out

        report_date = datetime.now().strftime('%Y-%m-%d')
        
        # 按评分排序
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)
        
        # 统计 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        
        lines = [
            f"## 🎯 {report_date} 决策仪表盘",
            "",
            f"> {len(results)}只股票 | 🟢买入:{buy_count} 🟡观望:{hold_count} 🔴卖出:{sell_count}",
            "",
        ]
        
        # Issue #262: summary_only 时仅输出摘要列表
        if self._report_summary_only:
            lines.append("**📊 分析结果摘要**")
            lines.append("")
            for r in sorted_results:
                _, signal_emoji, _ = self._get_signal_level(r)
                stock_name = self._escape_md(r.name if r.name and not r.name.startswith('股票') else f'股票{r.code}')
                lines.append(
                    f"{signal_emoji} **{stock_name}({r.code})**: {r.operation_advice} | "
                    f"评分 {r.sentiment_score} | {r.trend_prediction}"
                )
        else:
            for result in sorted_results:
                signal_text, signal_emoji, _ = self._get_signal_level(result)
                dashboard = result.dashboard if hasattr(result, 'dashboard') and result.dashboard else {}
                core = dashboard.get('core_conclusion', {}) if dashboard else {}
                battle = dashboard.get('battle_plan', {}) if dashboard else {}
                intel = dashboard.get('intelligence', {}) if dashboard else {}
                
                # 股票名称
                stock_name = result.name if result.name and not result.name.startswith('股票') else f'股票{result.code}'
                stock_name = self._escape_md(stock_name)
                
                # 标题行：信号等级 + 股票名称
                lines.append(f"### {signal_emoji} **{signal_text}** | {stock_name}({result.code})")
                lines.append("")
                
                # 核心决策（一句话）
                one_sentence = core.get('one_sentence', result.analysis_summary) if core else result.analysis_summary
                if one_sentence:
                    lines.append(f"📌 **{one_sentence[:80]}**")
                    lines.append("")
                
                # 重要信息区（舆情+基本面）
                info_lines = []
                
                # 业绩预期
                if intel.get('earnings_outlook'):
                    outlook = str(intel['earnings_outlook'])[:60]
                    info_lines.append(f"📊 业绩: {outlook}")
                if intel.get('sentiment_summary'):
                    sentiment = str(intel['sentiment_summary'])[:50]
                    info_lines.append(f"💭 舆情: {sentiment}")
                if info_lines:
                    lines.extend(info_lines)
                    lines.append("")
                
                # 风险警报（最重要，醒目显示）
                risks = intel.get('risk_alerts', []) if intel else []
                if risks:
                    lines.append("🚨 **风险**:")
                    for risk in risks[:2]:  # 最多显示2条
                        risk_str = str(risk)
                        risk_text = risk_str[:50] + "..." if len(risk_str) > 50 else risk_str
                        lines.append(f"   • {risk_text}")
                    lines.append("")
                
                # 利好催化
                catalysts = intel.get('positive_catalysts', []) if intel else []
                if catalysts:
                    lines.append("✨ **利好**:")
                    for cat in catalysts[:2]:  # 最多显示2条
                        cat_str = str(cat)
                        cat_text = cat_str[:50] + "..." if len(cat_str) > 50 else cat_str
                        lines.append(f"   • {cat_text}")
                    lines.append("")
                
                # 狙击点位
                sniper = battle.get('sniper_points', {}) if battle else {}
                if sniper:
                    ideal_buy = str(sniper.get('ideal_buy', ''))
                    stop_loss = str(sniper.get('stop_loss', ''))
                    take_profit = str(sniper.get('take_profit', ''))
                    points = []
                    if ideal_buy:
                        points.append(f"🎯买点:{ideal_buy[:15]}")
                    if stop_loss:
                        points.append(f"🛑止损:{stop_loss[:15]}")
                    if take_profit:
                        points.append(f"🎊目标:{take_profit[:15]}")
                    if points:
                        lines.append(" | ".join(points))
                        lines.append("")
                
                # 持仓建议
                pos_advice = core.get('position_advice', {}) if core else {}
                if pos_advice:
                    no_pos = str(pos_advice.get('no_position', ''))
                    has_pos = str(pos_advice.get('has_position', ''))
                    if no_pos:
                        lines.append(f"🆕 空仓者: {no_pos[:50]}")
                    if has_pos:
                        lines.append(f"💼 持仓者: {has_pos[:50]}")
                    lines.append("")
                
                # 检查清单简化版
                checklist = battle.get('action_checklist', []) if battle else []
                if checklist:
                    # 只显示不通过的项目
                    failed_checks = [str(c) for c in checklist if str(c).startswith('❌') or str(c).startswith('⚠️')]
                    if failed_checks:
                        lines.append("**检查未通过项**:")
                        for check in failed_checks[:3]:
                            lines.append(f"   {check[:40]}")
                        lines.append("")
                
                lines.append("---")
                lines.append("")
        
        # 底部
        lines.append(f"*生成时间: {datetime.now().strftime('%H:%M')}*")
        models = self._collect_models_used(results)
        if models:
            lines.append(f"*分析模型: {', '.join(models)}*")

        content = "\n".join(lines)
        
        return content
    
    def generate_wechat_summary(self, results: List[AnalysisResult]) -> str:
        """
        生成企业微信精简版日报（控制在4000字符内）

        Args:
            results: 分析结果列表

        Returns:
            精简版 Markdown 内容
        """
        report_date = datetime.now().strftime('%Y-%m-%d')

        # 按评分排序
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)

        # 统计 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        avg_score = sum(r.sentiment_score for r in results) / len(results) if results else 0

        lines = [
            f"## 📅 {report_date} 股票分析报告",
            "",
            f"> 共 **{len(results)}** 只 | 🟢买入:{buy_count} 🟡持有:{hold_count} 🔴卖出:{sell_count} | 均分:{avg_score:.0f}",
            "",
        ]
        
        # 每只股票精简信息（控制长度）
        for result in sorted_results:
            emoji = result.get_emoji()
            
            # 核心信息行
            lines.append(f"### {emoji} {result.name}({result.code})")
            lines.append(f"**{result.operation_advice}** | 评分:{result.sentiment_score} | {result.trend_prediction}")
            
            # 操作理由（截断）
            if hasattr(result, 'buy_reason') and result.buy_reason:
                reason = result.buy_reason[:80] + "..." if len(result.buy_reason) > 80 else result.buy_reason
                lines.append(f"💡 {reason}")
            
            # 核心看点
            if hasattr(result, 'key_points') and result.key_points:
                points = result.key_points[:60] + "..." if len(result.key_points) > 60 else result.key_points
                lines.append(f"🎯 {points}")
            
            # 风险提示（截断）
            if hasattr(result, 'risk_warning') and result.risk_warning:
                risk = result.risk_warning[:50] + "..." if len(result.risk_warning) > 50 else result.risk_warning
                lines.append(f"⚠️ {risk}")
            
            lines.append("")
        
        # 底部（模型行在 --- 之前，Issue #528）
        models = self._collect_models_used(results)
        if models:
            lines.append(f"*分析模型: {', '.join(models)}*")
        lines.extend([
            "---",
            "*AI生成，仅供参考，不构成投资建议*",
            f"*详细报告见 reports/report_{report_date.replace('-', '')}.md*"
        ])

        content = "\n".join(lines)

        return content

    def generate_brief_report(
        self,
        results: List[AnalysisResult],
        report_date: Optional[str] = None,
    ) -> str:
        """
        Generate brief report (3-5 sentences per stock) for mobile/push.

        Args:
            results: Analysis results list (use [result] for single stock).
            report_date: Report date (default: today).

        Returns:
            Brief markdown content.
        """
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')
        config = get_config()
        if getattr(config, 'report_renderer_enabled', False) and results:
            from src.services.report_renderer import render
            out = render(
                platform='brief',
                results=results,
                report_date=report_date,
                summary_only=False,
            )
            if out:
                return out
        # Fallback: brief summary from dashboard report
        if not results:
            return f"# {report_date} 决策简报\n\n无分析结果"
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        lines = [
            f"# {report_date} 决策简报",
            "",
            f"> {len(results)}只 | 🟢{buy_count} 🟡{hold_count} 🔴{sell_count}",
            "",
        ]
        for r in sorted_results:
            _, emoji, _ = self._get_signal_level(r)
            name = r.name if r.name and not r.name.startswith('股票') else f'股票{r.code}'
            dash = r.dashboard or {}
            core = dash.get('core_conclusion', {}) or {}
            one = (core.get('one_sentence') or r.analysis_summary or '')[:60]
            lines.append(f"**{self._escape_md(name)}({r.code})** {emoji} {r.operation_advice} | 评分{r.sentiment_score} | {one}")
        lines.append("")
        lines.append(f"*{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        return "\n".join(lines)

    def generate_single_stock_report(self, result: AnalysisResult) -> str:
        """
        生成单只股票的分析报告（用于单股推送模式 #55）
        
        格式精简但信息完整，适合每分析完一只股票立即推送
        
        Args:
            result: 单只股票的分析结果
            
        Returns:
            Markdown 格式的单股报告
        """
        report_date = datetime.now().strftime('%Y-%m-%d %H:%M')
        signal_text, signal_emoji, _ = self._get_signal_level(result)
        dashboard = result.dashboard if hasattr(result, 'dashboard') and result.dashboard else {}
        core = dashboard.get('core_conclusion', {}) if dashboard else {}
        battle = dashboard.get('battle_plan', {}) if dashboard else {}
        intel = dashboard.get('intelligence', {}) if dashboard else {}
        
        # 股票名称（转义 *ST 等特殊字符）
        raw_name = result.name if result.name and not result.name.startswith('股票') else f'股票{result.code}'
        stock_name = self._escape_md(raw_name)
        
        lines = [
            f"## {signal_emoji} {stock_name} ({result.code})",
            "",
            f"> {report_date} | 评分: **{result.sentiment_score}** | {result.trend_prediction}",
            "",
        ]

        self._append_market_snapshot(lines, result)
        
        # 核心决策（一句话）
        one_sentence = core.get('one_sentence', result.analysis_summary) if core else result.analysis_summary
        if one_sentence:
            lines.extend([
                "### 📌 核心结论",
                "",
                f"**{signal_text}**: {one_sentence}",
                "",
            ])
        
        # 重要信息（舆情+基本面）
        info_added = False
        if intel:
            if intel.get('earnings_outlook'):
                if not info_added:
                    lines.append("### 📰 重要信息")
                    lines.append("")
                    info_added = True
                lines.append(f"📊 **业绩预期**: {str(intel['earnings_outlook'])[:100]}")
            
            if intel.get('sentiment_summary'):
                if not info_added:
                    lines.append("### 📰 重要信息")
                    lines.append("")
                    info_added = True
                lines.append(f"💭 **舆情情绪**: {str(intel['sentiment_summary'])[:80]}")
            
            # 风险警报
            risks = intel.get('risk_alerts', [])
            if risks:
                if not info_added:
                    lines.append("### 📰 重要信息")
                    lines.append("")
                    info_added = True
                lines.append("")
                lines.append("🚨 **风险警报**:")
                for risk in risks[:3]:
                    lines.append(f"- {str(risk)[:60]}")
            
            # 利好催化
            catalysts = intel.get('positive_catalysts', [])
            if catalysts:
                lines.append("")
                lines.append("✨ **利好催化**:")
                for cat in catalysts[:3]:
                    lines.append(f"- {str(cat)[:60]}")
        
        if info_added:
            lines.append("")
        
        # 狙击点位
        sniper = battle.get('sniper_points', {}) if battle else {}
        if sniper:
            lines.extend([
                "### 🎯 操作点位",
                "",
                "| 买点 | 止损 | 目标 |",
                "|------|------|------|",
            ])
            ideal_buy = sniper.get('ideal_buy', '-')
            stop_loss = sniper.get('stop_loss', '-')
            take_profit = sniper.get('take_profit', '-')
            lines.append(f"| {ideal_buy} | {stop_loss} | {take_profit} |")
            lines.append("")
        
        # 持仓建议
        pos_advice = core.get('position_advice', {}) if core else {}
        if pos_advice:
            lines.extend([
                "### 💼 持仓建议",
                "",
                f"- 🆕 **空仓者**: {pos_advice.get('no_position', result.operation_advice)}",
                f"- 💼 **持仓者**: {pos_advice.get('has_position', '继续持有')}",
                "",
            ])
        
        lines.append("---")
        model_used = normalize_model_used(getattr(result, "model_used", None))
        if model_used:
            lines.append(f"*分析模型: {model_used}*")
        lines.append("*AI生成，仅供参考，不构成投资建议*")

        return "\n".join(lines)

    # Display name mapping for realtime data sources
    _SOURCE_DISPLAY_NAMES = {
        "tencent": "腾讯财经",
        "akshare_em": "东方财富",
        "akshare_sina": "新浪财经",
        "akshare_qq": "腾讯财经",
        "efinance": "东方财富(efinance)",
        "tushare": "Tushare Pro",
        "sina": "新浪财经",
        "fallback": "降级兜底",
    }

    def _append_market_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'market_snapshot', None)
        if not snapshot:
            return

        lines.extend([
            "### 📈 当日行情",
            "",
            "| 收盘 | 昨收 | 开盘 | 最高 | 最低 | 涨跌幅 | 涨跌额 | 振幅 | 成交量 | 成交额 |",
            "|------|------|------|------|------|-------|-------|------|--------|--------|",
            f"| {snapshot.get('close', 'N/A')} | {snapshot.get('prev_close', 'N/A')} | "
            f"{snapshot.get('open', 'N/A')} | {snapshot.get('high', 'N/A')} | "
            f"{snapshot.get('low', 'N/A')} | {snapshot.get('pct_chg', 'N/A')} | "
            f"{snapshot.get('change_amount', 'N/A')} | {snapshot.get('amplitude', 'N/A')} | "
            f"{snapshot.get('volume', 'N/A')} | {snapshot.get('amount', 'N/A')} |",
        ])

        if "price" in snapshot:
            raw_source = snapshot.get('source', 'N/A')
            display_source = self._SOURCE_DISPLAY_NAMES.get(raw_source, raw_source)
            lines.extend([
                "",
                "| 当前价 | 量比 | 换手率 | 行情来源 |",
                "|-------|------|--------|----------|",
                f"| {snapshot.get('price', 'N/A')} | {snapshot.get('volume_ratio', 'N/A')} | "
                f"{snapshot.get('turnover_rate', 'N/A')} | {display_source} |",
            ])

        lines.append("")

    def _should_use_image_for_channel(
        self, channel: NotificationChannel, image_bytes: Optional[bytes]
    ) -> bool:
        """
        Decide whether to send as image for the given channel (Issue #289).

        Fallback rules (send as Markdown text instead of image):
        - image_bytes is None: conversion failed / imgkit not installed / content over max_chars
        - WeChat: image exceeds ~2MB limit
        """
        if channel.value not in self._markdown_to_image_channels or image_bytes is None:
            return False
        if channel == NotificationChannel.WECHAT and len(image_bytes) > WECHAT_IMAGE_MAX_BYTES:
            logger.warning(
                "企业微信图片超限 (%d bytes)，回退为 Markdown 文本发送",
                len(image_bytes),
            )
            return False
        return True

    def send(
        self,
        content: str,
        email_stock_codes: Optional[List[str]] = None,
        email_send_to_all: bool = False
    ) -> bool:
        """
        统一发送接口 - 只向邮件渠道发送

        Args:
            content: 消息内容（Markdown 格式）
            email_stock_codes: 股票代码列表（可选，用于邮件渠道路由到对应分组邮箱，Issue #268）
            email_send_to_all: 邮件是否发往所有配置邮箱（用于大盘复盘等无股票归属的内容）

        Returns:
            是否发送成功
        """
        if not self._available_channels:
            logger.warning("通知服务不可用，跳过推送")
            return False

        # Markdown to image (Issue #289): convert once if email channel needs it.
        image_bytes = None
        if 'email' in self._markdown_to_image_channels:
            from src.md2img import markdown_to_image
            image_bytes = markdown_to_image(
                content, max_chars=self._markdown_to_image_max_chars
            )
            if image_bytes:
                logger.info("Markdown 已转换为图片，将向邮件发送图片")
            else:
                try:
                    from src.config import get_config
                    engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
                except Exception:
                    engine = "wkhtmltoimage"
                hint = (
                    "npm i -g markdown-to-file" if engine == "markdown-to-file"
                    else "wkhtmltopdf (apt install wkhtmltopdf / brew install wkhtmltopdf)"
                )
                logger.warning(
                    "Markdown 转图片失败，将回退为文本发送。请检查 MARKDOWN_TO_IMAGE_CHANNELS 配置并安装 %s",
                    hint,
                )

        channel_names = self.get_channel_names()
        logger.info(f"正在向 {len(self._available_channels)} 个渠道发送通知：{channel_names}")

        success_count = 0
        fail_count = 0

        for channel in self._available_channels:
            channel_name = ChannelDetector.get_channel_name(channel)
            use_image = 'email' in self._markdown_to_image_channels and image_bytes is not None
            try:
                if channel == NotificationChannel.EMAIL:
                    receivers = None
                    if email_send_to_all and self._stock_email_groups:
                        receivers = self.get_all_email_receivers()
                    elif email_stock_codes and self._stock_email_groups:
                        receivers = self.get_receivers_for_stocks(email_stock_codes)
                    if use_image:
                        result = self._send_email_with_inline_image(
                            image_bytes, receivers=receivers
                        )
                    else:
                        result = self.send_to_email(content, receivers=receivers)
                else:
                    logger.warning(f"不支持的通知渠道: {channel}")
                    result = False

                if result:
                    success_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logger.error(f"{channel_name} 发送失败: {e}")
                fail_count += 1

        logger.info(f"通知发送完成：成功 {success_count} 个，失败 {fail_count} 个")
        return success_count > 0
   
    def save_report_to_file(
        self, 
        content: str, 
        filename: Optional[str] = None
    ) -> str:
        """
        保存日报到本地文件
        
        Args:
            content: 日报内容
            filename: 文件名（可选，默认按日期生成）
            
        Returns:
            保存的文件路径
        """
        from pathlib import Path
        
        if filename is None:
            date_str = datetime.now().strftime('%Y%m%d')
            filename = f"report_{date_str}.md"
        
        # 确保 reports 目录存在（使用项目根目录下的 reports）
        reports_dir = Path(__file__).parent.parent / 'reports'
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        filepath = reports_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.info(f"日报已保存到: {filepath}")
        return str(filepath)


class NotificationBuilder:
    """
    通知消息构建器
    
    提供便捷的消息构建方法
    """
    
    @staticmethod
    def build_simple_alert(
        title: str,
        content: str,
        alert_type: str = "info"
    ) -> str:
        """
        构建简单的提醒消息
        
        Args:
            title: 标题
            content: 内容
            alert_type: 类型（info, warning, error, success）
        """
        emoji_map = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
            "success": "✅",
        }
        emoji = emoji_map.get(alert_type, "📢")
        
        return f"{emoji} **{title}**\n\n{content}"
    
    @staticmethod
    def build_stock_summary(results: List[AnalysisResult]) -> str:
        """
        构建股票摘要（简短版）
        
        适用于快速通知
        """
        lines = ["📊 **今日自选股摘要**", ""]
        
        for r in sorted(results, key=lambda x: x.sentiment_score, reverse=True):
            emoji = r.get_emoji()
            lines.append(f"{emoji} {r.name}({r.code}): {r.operation_advice} | 评分 {r.sentiment_score}")
        
        return "\n".join(lines)


# 便捷函数
def get_notification_service() -> NotificationService:
    """获取通知服务实例"""
    return NotificationService()


def send_daily_report(results: List[AnalysisResult]) -> bool:
    """
    发送每日报告的快捷方式
    
    自动识别渠道并推送
    """
    service = get_notification_service()
    
    # 生成报告
    report = service.generate_daily_report(results)
    
    # 保存到本地
    service.save_report_to_file(report)
    
    # 推送到配置的渠道（自动识别）
    return service.send(report)


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    # 模拟分析结果
    test_results = [
        AnalysisResult(
            code='600519',
            name='贵州茅台',
            sentiment_score=75,
            trend_prediction='看多',
            analysis_summary='技术面强势，消息面利好',
            operation_advice='买入',
            technical_analysis='放量突破 MA20，MACD 金叉',
            news_summary='公司发布分红公告，业绩超预期',
        ),
        AnalysisResult(
            code='000001',
            name='平安银行',
            sentiment_score=45,
            trend_prediction='震荡',
            analysis_summary='横盘整理，等待方向',
            operation_advice='持有',
            technical_analysis='均线粘合，成交量萎缩',
            news_summary='近期无重大消息',
        ),
        AnalysisResult(
            code='300750',
            name='宁德时代',
            sentiment_score=35,
            trend_prediction='看空',
            analysis_summary='技术面走弱，注意风险',
            operation_advice='卖出',
            technical_analysis='跌破 MA10 支撑，量能不足',
            news_summary='行业竞争加剧，毛利率承压',
        ),
    ]
    
    service = NotificationService()
    
    # 显示检测到的渠道
    print("=== 通知渠道检测 ===")
    print(f"当前渠道: {service.get_channel_names()}")
    print(f"渠道列表: {service.get_available_channels()}")
    print(f"服务可用: {service.is_available()}")
    
    # 生成日报
    print("\n=== 生成日报测试 ===")
    report = service.generate_daily_report(test_results)
    print(report)
    
    # 保存到文件
    print("\n=== 保存日报 ===")
    filepath = service.save_report_to_file(report)
    print(f"保存成功: {filepath}")
    
    # 推送测试
    if service.is_available():
        print(f"\n=== 推送测试（{service.get_channel_names()}）===")
        success = service.send(report)
        print(f"推送结果: {'成功' if success else '失败'}")
    else:
        print("\n通知渠道未配置，跳过推送测试")
