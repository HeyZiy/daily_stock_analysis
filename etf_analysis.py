# -*- coding: utf-8 -*-
"""
===================================
ETF 分析主程序
===================================

功能：
1. 分析18只ETF的技术面和动量
2. 生成资产配置建议
3. 发送通知（可选）
4. 保存报告

使用方式：
    python etf_analysis.py              # 正常运行
    python etf_analysis.py --debug      # 调试模式
    python etf_analysis.py --no-notify  # 不发送通知
"""
import argparse
import logging
import os
import sys
from datetime import datetime

from src.logging_config import setup_logging
from src.notification import NotificationService
from src.etf_analyzer import ETFAnalyzer, generate_etf_report

logger = logging.getLogger(__name__)


def _save_report(report: str) -> str:
    """保存报告到文件"""
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    report_path = os.path.join(reports_dir, f"etf_analysis_{today_str}.md")
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info(f"报告已保存: {report_path}")
    return report_path


def _send_notification(report: str) -> bool:
    """发送通知"""
    notifier = NotificationService()
    if not notifier.is_available():
        logger.warning("通知服务未配置")
        return False
    
    success = notifier.send(report)
    if success:
        logger.info("通知发送成功")
    else:
        logger.warning("通知发送失败")
    
    return success


def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description='ETF分析程序 - 分析18只精选ETF',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试模式'
    )
    parser.add_argument(
        '--no-notify',
        action='store_true',
        help='不发送推送通知'
    )
    parser.add_argument(
        '--single',
        type=str,
        help='只分析单个ETF代码'
    )
    
    args = parser.parse_args()
    
    # 配置日志
    setup_logging(log_prefix="etf_analysis", debug=args.debug)
    
    logger.info("=" * 60)
    logger.info("ETF 分析程序启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    try:
        analyzer = ETFAnalyzer()
        
        if args.single:
            # 单个ETF分析模式
            from src.etf_config import get_etf_by_code
            etf = get_etf_by_code(args.single)
            if etf:
                logger.info(f"分析单个ETF: {etf.name}({etf.code})")
                perf = analyzer.analyze_single_etf(etf)
                if perf:
                    print(f"\n{etf.name}({etf.code}) 分析结果:")
                    print(f"  当前价格: {perf.current_price:.3f}")
                    print(f"  涨跌幅: 1日{perf.change_1d:+.2f}% | 5日{perf.change_5d:+.2f}% | 20日{perf.change_20d:+.2f}%")
                    print(f"  趋势: {perf.trend_result.trend_status.value if perf.trend_result else 'N/A'}")
                    print(f"  评分: {perf.score} - {perf.suggestion}")
            else:
                logger.error(f"找不到ETF代码: {args.single}")
                return 1
        else:
            # 完整分析模式
            logger.info("开始分析所有ETF...")
            report = generate_etf_report(analyzer)
            
            # 输出报告
            print("\n" + "=" * 60)
            print("ETF 分析报告")
            print("=" * 60)
            print(report)
            
            # 保存报告
            report_path = _save_report(report)
            
            # 发送通知
            if not args.no_notify:
                _send_notification(report)
        
        logger.info("ETF分析完成")
        return 0
        
    except Exception as e:
        logger.exception(f"ETF分析失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
