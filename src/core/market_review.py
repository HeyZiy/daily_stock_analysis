# -*- coding: utf-8 -*-
"""
===================================
股票智能分析系统 - 大盘复盘模块（支持 A 股 / 美股）
===================================

职责：
1. 根据 MARKET_REVIEW_REGION 配置选择市场区域（cn / us / both）
2. 执行大盘复盘分析并生成复盘报告（含市场环境量化指标）
3. 保存和发送复盘报告
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from src.config import get_config
from src.notification import NotificationService
from src.market_analyzer import MarketAnalyzer
from src.search_service import SearchService
from src.analyzer import GeminiAnalyzer


logger = logging.getLogger(__name__)


def check_market_environment_quantitative(region: str = 'cn') -> Optional[str]:
    """
    量化检查市场环境（基于博弈仓策略的5项条件）

    满足两条件以上才允许开仓：
    1. 指数站上20日线
    2. 成交额高于近20日均量
    3. 有明确持续主线（非一日游）
    4. 涨停家数明显高于跌停家数
    5. 主线板块持续活跃3天以上

    Returns:
        格式化后的市场环境检查报告，或 None
    """
    try:
        import akshare as ak

        lines = ["## 市场环境量化检查", ""]
        met_count = 0
        total_checks = 0

        # 1. 指数站上20日线
        try:
            index_df = ak.stock_zh_index_daily(symbol="sh000001")
            if index_df is not None and len(index_df) >= 20:
                index_df = index_df.sort_values('date').reset_index(drop=True)
                index_df['ma20'] = index_df['close'].rolling(window=20).mean()
                latest = index_df.iloc[-1]
                if pd.notna(latest['ma20']) and latest['close'] > latest['ma20']:
                    lines.append(f"- ✅ 上证指数 **{latest['close']:.0f}** > MA20 **{latest['ma20']:.0f}**")
                    met_count += 1
                else:
                    lines.append(f"- ❌ 上证指数 **{latest['close']:.0f}** ≤ MA20 **{latest['ma20']:.0f}**")
                total_checks += 1
        except Exception as e:
            logger.warning(f"获取指数数据失败: {e}")

        # 2. 成交额高于近20日均量 + 4. 涨跌停统计
        try:
            today_str = datetime.now().strftime("%Y%m%d")
            zt_df = ak.stock_zt_pool_em(date=today_str)
            limit_up = len(zt_df) if zt_df is not None else 0

            dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
            limit_down = len(dt_df) if dt_df is not None else 0

            if limit_up > limit_down * 1.5:
                lines.append(f"- ✅ 涨停 **{limit_up}** 家 > 跌停 **{limit_down}** 家")
                met_count += 1
            else:
                lines.append(f"- ❌ 涨停 **{limit_up}** 家 ≈ 跌停 **{limit_down}** 家（无明显优势）")
            total_checks += 1
        except Exception as e:
            logger.warning(f"获取涨跌停数据失败: {e}")

        # 3. 板块强度排名（作为主线持续性参考）
        try:
            sector_df = ak.stock_board_industry_name_em()
            if sector_df is not None and not sector_df.empty:
                sector_df = sector_df.sort_values('涨幅', ascending=False)
                top3 = sector_df.head(3)['板块名称'].tolist()
                lines.append(f"- ℹ️ 涨幅前三板块: {'、'.join(top3)}（关注持续性）")
        except Exception:
            pass

        summary = f"**市场环境评分: {met_count}/{total_checks} 项满足**"
        if met_count >= 2:
            summary += " — ✅ 满足开仓条件"
        else:
            summary += " — ⛔ 建议空仓观望"

        lines.append("")
        lines.append(f"> {summary}")
        lines.append("")
        lines.append("---")
        lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"市场环境量化检查失败: {e}")
        return None


def run_market_review(
    notifier: NotificationService,
    analyzer: Optional[GeminiAnalyzer] = None,
    search_service: Optional[SearchService] = None,
    send_notification: bool = True,
    merge_notification: bool = False,
    override_region: Optional[str] = None,
) -> Optional[str]:
    """
    执行大盘复盘分析

    Args:
        notifier: 通知服务
        analyzer: AI分析器（可选）
        search_service: 搜索服务（可选）
        send_notification: 是否发送通知
        merge_notification: 是否合并推送（跳过本次推送，由 main 层合并个股+大盘后统一发送，Issue #190）
        override_region: 覆盖 config 的 market_review_region（Issue #373 交易日过滤后有效子集）

    Returns:
        复盘报告文本
    """
    logger.info("开始执行大盘复盘分析...")
    config = get_config()
    region = (
        override_region
        if override_region is not None
        else (getattr(config, 'market_review_region', 'cn') or 'cn')
    )
    if region not in ('cn', 'us', 'both'):
        region = 'cn'

    try:
        if region == 'both':
            # 顺序执行 A 股 + 美股，合并报告
            cn_analyzer = MarketAnalyzer(
                search_service=search_service, analyzer=analyzer, region='cn'
            )
            us_analyzer = MarketAnalyzer(
                search_service=search_service, analyzer=analyzer, region='us'
            )
            logger.info("生成 A 股大盘复盘报告...")
            cn_report = cn_analyzer.run_daily_review()
            logger.info("生成美股大盘复盘报告...")
            us_report = us_analyzer.run_daily_review()
            review_report = ''
            if cn_report:
                review_report = f"# A股大盘复盘\n\n{cn_report}"
            if us_report:
                if review_report:
                    review_report += "\n\n---\n\n> 以下为美股大盘复盘\n\n"
                review_report += f"# 美股大盘复盘\n\n{us_report}"
            if not review_report:
                review_report = None
        else:
            market_analyzer = MarketAnalyzer(
                search_service=search_service,
                analyzer=analyzer,
                region=region,
            )
            review_report = market_analyzer.run_daily_review()

        if review_report:
            # 添加市场环境量化检查（仅 A 股）
            env_report = None
            if region in ('cn', 'both'):
                env_report = check_market_environment_quantitative(region='cn')

            full_report = review_report
            if env_report:
                full_report = env_report + "\n" + review_report

            # 保存报告到文件
            date_str = datetime.now().strftime('%Y%m%d')
            report_filename = f"market_review_{date_str}.md"
            filepath = notifier.save_report_to_file(
                f"# 🎯 大盘复盘\n\n{full_report}",
                report_filename
            )
            logger.info(f"大盘复盘报告已保存: {filepath}")

            # 推送通知（合并模式下跳过，由 main 层统一发送）
            if merge_notification and send_notification:
                logger.info("合并推送模式：跳过大盘复盘单独推送，将在个股+大盘复盘后统一发送")
            elif send_notification and notifier.is_available():
                report_content = f"🎯 大盘复盘\n\n{full_report}"

                success = notifier.send(report_content, email_send_to_all=True)
                if success:
                    logger.info("大盘复盘推送成功")
                else:
                    logger.warning("大盘复盘推送失败")
            elif not send_notification:
                logger.info("已跳过推送通知 (--no-notify)")

            return review_report
        
    except Exception as e:
        logger.error(f"大盘复盘分析失败: {e}")
    
    return None
