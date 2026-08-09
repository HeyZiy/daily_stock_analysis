# -*- coding: utf-8 -*-
"""
===================================
策略规划器 — Strategy Planner
===================================

根据市场状态诊断，规划当前阶段应使用的策略及其权重配置。

职责：
1. 收集市场数据（指数、板块、宏观、情绪）
2. LLM 诊断当前市场阶段
3. LLM 逐策略分析适配度
4. LLM 提议新策略（自进化）
5. 生成报告 + 通知

使用方式：
    python strategy_planner.py                    # 正常运行
    python strategy_planner.py --debug            # 调试模式
    python strategy_planner.py --no-notify        # 不发送通知
    python strategy_planner.py --no-llm           # 跳过 LLM 分析，仅采集数据
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from src.config import setup_env

setup_env()

from src.logging_config import setup_logging

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="策略规划器 — 根据市场状态规划策略配置")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    parser.add_argument("--no-llm", action="store_true", help="跳过LLM分析，仅采集并打印数据")
    parser.add_argument("--list-strategies", action="store_true", help="列出当前策略池")
    parser.add_argument("--list-pending", action="store_true", help="列出待审批策略")
    parser.add_argument("--approve", type=str, metavar="STRATEGY_ID", help="批准指定策略")
    parser.add_argument("--remove", type=str, metavar="STRATEGY_ID", help="移除指定策略")
    args = parser.parse_args()

    setup_logging(log_prefix="strategy_planner", debug=args.debug)

    # 策略管理命令
    if args.list_strategies:
        from src.strategy_planner.strategy_registry import get_all_strategies
        strategies = get_all_strategies()
        print(f"\n当前策略池（共 {len(strategies)} 个）:\n")
        for i, s in enumerate(strategies, 1):
            print(f"  {i}. [{s.get('category', 'N/A')}] {s['name']} (id={s['id']})")
            print(f"     适配: {', '.join(s.get('suitable_regimes', []))}")
        return

    if args.list_pending:
        from src.strategy_planner.strategy_registry import get_pending_strategies
        pending = get_pending_strategies()
        print(f"\n待审批策略（共 {len(pending)} 个）:\n")
        for i, s in enumerate(pending, 1):
            print(f"  {i}. [{s.get('category', 'N/A')}] {s['name']} (id={s['id']})")
            print(f"     {s.get('description', '')}")
            print(f"     {s.get('why_now', '')}")
        return

    if args.approve:
        from src.strategy_planner.strategy_registry import approve_strategy
        result = approve_strategy(args.approve)
        if result:
            print(f"已批准策略: {result['name']}")
        else:
            print(f"未找到策略: {args.approve}")
        return

    if args.remove:
        from src.strategy_planner.strategy_registry import remove_strategy
        result = remove_strategy(args.remove)
        if result:
            print(f"已移除策略: {result['name']}")
        else:
            print(f"未找到策略: {args.remove}")
        return

    # ── 主流程 ──
    today_str = date.today().isoformat()
    weekday_str = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][date.today().weekday()]
    logger.info(f"====== 策略规划开始 ({today_str} {weekday_str}) ======")

    print(f"\n{'='*60}")
    print(f"  📊 策略规划 Agent")
    print(f"  {today_str} {weekday_str}")
    print(f"{'='*60}\n")

    # Step 1: 采集数据
    print("[1/4] 采集市场市场数据...")
    from src.strategy_planner.data_collector import collect_all_data, format_data_for_llm
    market_data = collect_all_data()
    data_text = format_data_for_llm(market_data)
    logger.info(f"数据采集完成，已获取 {len(market_data)} 个数据类别")

    # Step 2: 加载策略
    print("[2/4] 加载策略池...")
    from src.strategy_planner.strategy_registry import get_all_strategies, format_strategies_for_llm
    strategies = get_all_strategies()
    strategies_text = format_strategies_for_llm(strategies)
    logger.info(f"加载了 {len(strategies)} 个策略")

    if args.no_llm:
        print("\n" + "="*60)
        print(data_text)
        print("="*60)
        print("\n--no-llm 已指定，跳过 LLM 分析。")
        return

    # Step 3: LLM 分析
    print("[3/4] LLM 市场诊断 + 策略适配 + 策略进化...")
    from src.strategy_planner.analyzer import run_full_analysis
    from src.strategy_planner.llm_client import get_llm_client

    client = get_llm_client()
    if not client.available:
        logger.error("LLM 不可用！请检查 DEEPSEEK_API_KEY 或 LLM_CHANNELS 配置")
        print("\n❌ 错误: LLM 不可用。请设置 DEEPSEEK_API_KEY 环境变量或配置 LLM_CHANNELS。")
        print("   你可以使用 --no-llm 跳过 LLM 分析仅查看数据。")
        sys.exit(1)

    analysis = run_full_analysis(data_text, strategies_text)

    # 打印分析摘要
    diagnosis = analysis.get("诊断", {})
    print(f"   市场阶段: {diagnosis.get('phase', 'N/A')}")
    print(f"   风险等级: {diagnosis.get('risk_level', 'N/A')}")
    strategy_fit = analysis.get("策略适配", {})
    assessments = strategy_fit.get("strategy_assessments", [])
    if assessments:
        top_strategy = max(assessments, key=lambda x: x.get("fit_score", 0))
        print(f"   最适配策略: {top_strategy.get('strategy_name', 'N/A')} (适配度 {top_strategy.get('fit_score', 0)}/100)")
    evolution = analysis.get("策略进化", {})
    new_count = len(evolution.get("suggestions", []))
    print(f"   新策略提议: {new_count} 个")

    # Step 4: 生成报告
    print("[4/4] 生成市场报告...")
    from src.strategy_planner.report import generate_report
    report = generate_report(analysis, data_text)

    # 保存报告
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report_filename = f"strategy_planner_{today_str}.md"
    report_path = reports_dir / report_filename
    report_path.write_text(report, encoding="utf-8")
    logger.info(f"报告已保存: {report_path.resolve()}")

    # 发送通知
    if not args.no_notify:
        try:
            from src.notify.service import NotificationService
            from src.config import get_config
            config = get_config()

            subject = f"📊 策略规划 - {today_str} {weekday_str} - {diagnosis.get('phase', 'N/A')}"

            notify = NotificationService()
            notify.send_markdown(
                subject=subject,
                markdown_content=report,
                config=config,
            )
            logger.info("通知已发送")
        except Exception as e:
            logger.warning(f"通知发送失败: {e}")

    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"  ✅ 分析完成！")
    print(f"  报告: {report_path.resolve()}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
