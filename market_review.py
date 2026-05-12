# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 大盘复盘独立入口
===================================

职责：
1. 提供独立 CLI 入口运行大盘复盘
2. 封装组件初始化与交易日过滤逻辑
3. 可被导入为模块使用，也可直接运行

使用方式：
    python market_review.py              # 正常运行
    python market_review.py --region us  # 美股大盘
    python market_review.py --no-notify  # 不推送通知
    python market_review.py --force-run  # 跳过交易日检查
"""

import sys
import argparse
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple

import pandas as pd

from src.config import setup_env, get_config, Config
from src.logging_config import setup_logging
from src.market_analyzer import MarketAnalyzer
from src.services.mx_service import MXService

setup_env()

from src.notification import NotificationService
from src.core.trading_calendar import get_open_markets_today, compute_effective_region

logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(
        description='大盘复盘独立入口',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python market_review.py                  # 默认地区，正常运行
  python market_review.py --region us      # 美股大盘
  python market_review.py --no-notify      # 不发送推送
  python market_review.py --force-run      # 跳过交易日检查
  python market_review.py --debug          # 调试模式
""")

    parser.add_argument('--debug',
                        action='store_true',
                        help='启用调试模式，输出详细日志')

    parser.add_argument('--region',
                        choices=['cn', 'us', 'both'],
                        default=None,
                        help='指定市场地区 (cn/us/both，默认从 config 读取)')

    parser.add_argument('--no-notify',
                        action='store_true',
                        help='不发送推送通知')

    parser.add_argument('--force-run',
                        action='store_true',
                        help='跳过交易日检查，强制执行')

    return parser.parse_args()


def run_market_review_standalone(
    config: Config = None,
    region: str = None,
    no_notify: bool = False,
    force_run: bool = False,
    notifier: NotificationService = None,
) -> str:
    """
    运行独立大盘复盘（可作为函数导入使用）

    Args:
        config: 配置对象（可选）
        region: 覆盖地区 cn/us/both（可选）
        no_notify: 不发送通知（可选）
        force_run: 跳过交易日检查（可选）
        notifier: 复用外部通知服务（可选）

    Returns:
        复盘报告文本
    """
    config = config or get_config()

    # 交易日过滤（仅在独立模式下，force_run 跳过）
    effective_region = None
    if not force_run and config.trading_day_check_enabled:
        open_markets = get_open_markets_today()
        target_region = (
            region
            if region is not None
            else (getattr(config, 'market_review_region', 'cn') or 'cn')
        )
        effective_region = compute_effective_region(target_region, open_markets)
        if effective_region == '':
            logging.getLogger(__name__).info(
                '今日大盘复盘相关市场均为非交易日，跳过执行。'
            )
            return ''

    # 初始化通知服务（如果未传入外部组件）
    if notifier is None:
        notifier = NotificationService()

    # 调用大盘复盘核心（MX 搜索服务由 MarketAnalyzer 内部自动初始化）
    report = run_market_review(
        notifier=notifier,
        send_notification=not no_notify,
        override_region=effective_region if effective_region else region,
    )
    return report or ''


def main():
    args = parse_args()
    config = get_config()
    setup_logging(log_prefix='market_review', debug=args.debug, log_dir=config.log_dir)
    logger = logging.getLogger(__name__)
    logger.info('========== 大盘复盘独立模式启动 ==========')

    try:
        report = run_market_review_standalone(
            config=config,
            region=args.region,
            no_notify=args.no_notify,
            force_run=args.force_run,
        )
        if report:
            logger.info('========== 大盘复盘完成 ==========')
        return 0
    except Exception as e:
        logger.exception(f'大盘复盘执行异常: {e}')
        return 1


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
    send_notification: bool = True,
    merge_notification: bool = False,
    override_region: Optional[str] = None,
) -> Optional[str]:
    """
    执行大盘复盘分析

    Args:
        notifier: 通知服务
        send_notification: 是否发送通知
        merge_notification: 是否合并推送
        override_region: 覆盖 config 的 market_review_region

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
            cn_analyzer = MarketAnalyzer(region='cn')
            us_analyzer = MarketAnalyzer(region='us')
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
            market_analyzer = MarketAnalyzer(region=region)
            review_report = market_analyzer.run_daily_review()

        if review_report:
            env_report = None
            if region in ('cn', 'both'):
                env_report = check_market_environment_quantitative(region='cn')

            full_report = review_report
            if env_report:
                full_report = env_report + "\n" + review_report

            date_str = datetime.now().strftime('%Y%m%d')
            report_filename = f"market_review_{date_str}.md"
            filepath = notifier.save_report_to_file(
                f"# 🎯 大盘复盘\n\n{full_report}",
                report_filename
            )
            logger.info(f"大盘复盘报告已保存: {filepath}")

            if merge_notification and send_notification:
                logger.info("合并推送模式：跳过大盘复盘单独推送，将在个股+大盘复盘后统一发送")
            elif send_notification and notifier.is_available():
                success = notifier.send(f"🎯 大盘复盘\n\n{full_report}", email_send_to_all=True)
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


def _detect_regime(index_df, met_count: int) -> str:
    """根据指数均线状态和门控条件数判断当前市场状态

    Returns:
        trending_up   — 均线多头且门控条件足，趋势行情
        trending_down — 均线空头，控仓观望
        sideways      — 居中震荡栋
        unknown       — 无法判断，保守处理
    """
    import pandas as pd
    try:
        if index_df is None or len(index_df) < 20:
            return "unknown"
        ma5  = index_df['close'].rolling(5).mean().iloc[-1]
        ma10 = index_df['close'].rolling(10).mean().iloc[-1]
        ma20 = index_df['close'].rolling(20).mean().iloc[-1]
        close = index_df['close'].iloc[-1]
        if any(pd.isna(x) for x in [ma5, ma10, ma20]):
            return "unknown"
        if ma5 > ma10 > ma20 and close > ma10 and met_count >= 2:
            return "trending_up"
        if ma5 < ma10 < ma20 and close < ma10:
            return "trending_down"
        if abs(close - ma20) / ma20 < 0.03:
            return "sideways"
    except Exception:
        pass
    return "unknown"


def _parse_mx_theme_result(result: Optional[Dict]) -> Tuple[bool, str]:
    """解析 MX search_news 返回值，判断是否有明确市场主线

    Returns:
        (是否有主线, 描述文本)
    """
    if not result or result.get("status") != 0:
        return False, "妙想资讯查询无结果"

    data = result.get("data", {})
    inner = data.get("data", {})
    search_resp = inner.get("llmSearchResponse", {})
    items = search_resp.get("data", [])
    if not isinstance(items, list) or not items:
        return False, "未找到相关市场主线资讯"

    # 收集提及的板块/概念关键词
    sector_keywords = {"板块", "概念", "行业", "主线", "热点", "领涨",
                       "AI", "人工智能", "半导体", "新能源", "消费", "医药",
                       "金融", "科技", "芯片", "算力", "汽车", "光伏"}
    mentioned = set()
    titles = []
    for item in items:
        title = item.get("title", "")
        content = item.get("content", "")
        text = f"{title} {content}"
        titles.append(title)
        for kw in sector_keywords:
            if kw in text:
                mentioned.add(kw)

    desc = "；".join(titles[:3]) if titles else "未识别"
    # 有至少 2 条结果且提到了板块关键词 → 认为有主线
    has_theme = len(items) >= 2 and len(mentioned) >= 1
    return has_theme, desc


def check_market_gate(
    mx_service: Optional['MXService'] = None,
) -> Tuple[bool, Dict[str, bool], str, str]:
    """
    博弈仓策略 — 市场环境开仓门控

    满足两条件以上才允许开仓：
    1. 指数站上20日线（上证指数）
    2. 成交额高于近20日均量
    3. 有明确持续主线（非一日游）
    4. 涨停家数明显高于跌停家数
    5. 主线板块持续活跃3天以上

    Args:
        mx_service: 妙想 API 服务（可选），配置后用于定性判断条件 3 和 5

    Returns:
        (can_trade, conditions_dict, summary_str, regime)
    """
    import pandas as pd
    from datetime import date

    logger = logging.getLogger(__name__)

    conditions: Dict[str, bool] = {
        "指数站上20日线":   False,
        "成交额高于近20日均量": False,
        "有明确持续主线":   False,
        "涨停多于跌停":    False,
        "板块持续活跃3天":  False,
    }
    met_count = 0
    details = []

    try:
        import akshare as ak

        # 条件1 & 2：上证指数日线数据
        index_df = ak.stock_zh_index_daily(symbol="sh000001")
        if index_df is not None and len(index_df) >= 20:
            index_df = index_df.sort_values('date').reset_index(drop=True)
            index_df['ma20'] = index_df['close'].rolling(window=20).mean()
            latest_idx = index_df.iloc[-1]
            idx_close = latest_idx['close']
            idx_ma20 = latest_idx['ma20']

            if pd.notna(idx_ma20) and idx_close > idx_ma20:
                conditions["指数站上20日线"] = True
                met_count += 1
                details.append(f"✅ 上证指数{idx_close:.0f} > MA20{idx_ma20:.0f}")
            else:
                details.append(f"❌ 上证指数{idx_close:.0f} ≤ MA20{idx_ma20:.0f}")

            if 'amount' in index_df.columns:
                latest_amount = latest_idx.get('amount', 0)
                avg_amount = index_df['amount'].iloc[-20:].mean()
                if pd.notna(avg_amount) and avg_amount > 0 and latest_amount > avg_amount:
                    conditions["成交额高于近20日均量"] = True
                    met_count += 1
                    details.append(f"✅ 成交额{latest_amount/1e8:.0f}亿 > 20日均量{avg_amount/1e8:.0f}亿")
                else:
                    details.append(f"❌ 成交额{latest_amount/1e8:.0f}亿 ≤ 20日均量{avg_amount/1e8:.0f}亿")

        # 条件4：涨跌停数据
        try:
            today_str = date.today().strftime("%Y%m%d")
            zt_df = ak.stock_zt_pool_em(date=today_str)
            if zt_df is not None and not zt_df.empty:
                limit_up = len(zt_df)
                dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
                limit_down = len(dt_df) if dt_df is not None else 0
                if limit_up > limit_down * 1.5:
                    conditions["涨停多于跌停"] = True
                    met_count += 1
                    details.append(f"✅ 涨停{limit_up}家 > 跌停{limit_down}家")
                else:
                    details.append(f"⚠️ 涨停{limit_up}家 ≈ 跌停{limit_down}家（条件不满足）")
        except Exception:
            details.append("⚠️ 获取涨跌停数据失败，跳过此项")

        # 条件3 & 5：使用妙想资讯定性判断（可选）
        if mx_service and mx_service.api_key:
            theme_result = mx_service.search_news("今日A股市场主线板块")
            has_theme, theme_desc = _parse_mx_theme_result(theme_result)
            conditions["有明确持续主线"] = has_theme
            if has_theme:
                met_count += 1
                details.append(f"✅ 有明确持续主线: {theme_desc}")
            else:
                details.append(f"ℹ️ 未识别出明确主线: {theme_desc}")

            persist_result = mx_service.search_news("近三日持续活跃板块")
            has_persist, persist_desc = _parse_mx_theme_result(persist_result)
            conditions["板块持续活跃3天"] = has_persist
            if has_persist:
                met_count += 1
                details.append(f"✅ 板块持续活跃: {persist_desc}")
            else:
                details.append(f"ℹ️ 板块持续性不足: {persist_desc}")
        else:
            details.append("➖ 持续主线（未配置MX_APIKEY，跳过）")
            details.append("➖ 板块活跃3天（未配置MX_APIKEY，跳过）")

    except Exception as e:
        logger.error(f"市场门控检查失败: {e}")
        return True, conditions, "市场环境检查失败（默认放行）", "unknown"

    can_trade = met_count >= 2
    env_icon = "✅ 允许开仓" if can_trade else "❌ 建议空仓"
    summary = (
        f"市场环境检查：满足{met_count}/5项条件 → {env_icon}\n"
        + "\n".join(details)
    )

    regime = _detect_regime(index_df if len(index_df) > 0 else None, met_count)

    if can_trade:
        logger.info(f"✅ 市场环境满足开仓条件（{met_count}/5）")
    else:
        logger.warning(f"⛔ 市场环境不满足开仓条件（仅{met_count}/5），建议空仓")

    return can_trade, conditions, summary, regime


if __name__ == '__main__':
    sys.exit(main())
