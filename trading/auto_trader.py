# -*- coding: utf-8 -*-
"""
模拟盘自动交易入口

用法:
    # 盘后分析（收盘后运行，生成次日交易计划）
    python trading/auto_trader.py analyze
    
    # 盘中执行（开盘/盘中运行，检查买入卖出）
    python trading/auto_trader.py execute
    
    # 查看当前计划
    python trading/auto_trader.py plan
    
    # 一体化模式：分析 + 生成计划 + 微信通知
    python trading/auto_trader.py run
"""

import argparse
import io
import logging
import sys
from datetime import datetime, time
from typing import Dict, Optional

# 修复 Windows GBK 编码问题：让 stdout 支持 UTF-8 emoji 输出
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def setup_env():
    """初始化环境"""
    from src.config import setup_env as _setup_env
    _setup_env()


def run_analysis() -> str:
    """
    执行盘后分析
    
    流程：
    1. 运行 main.py 获取技术信号
    2. 筛选高优先级买入候选
    3. 生成次日交易计划
    4. 返回报告文本（用于微信通知）
    """
    from main import SimpleTechnicalAnalyzer

    logger.info("开始盘后分析...")

    analyzer = SimpleTechnicalAnalyzer()

    # 获取股票列表
    stock_codes, name_mapping = analyzer.mx_service.fetch_self_selected()
    if not stock_codes:
        return "今日无符合条件的信号"

    # 同步到本地数据库
    analyzer.sync_stocks_to_db(stock_codes, name_mapping)

    # 获取当前活跃的关注列表
    active_watch_list = analyzer.get_active_watch_list()
    if not active_watch_list:
        return "今日无符合条件的信号"

    # 技术分析（包含剔除检查）
    signals_df, removed_stocks = analyzer.analyze_all_stocks(active_watch_list)

    if removed_stocks:
        analyzer.remove_stocks(removed_stocks)

    # 将 signals 转为 DataFrame 兼容格式
    if not signals_df:
        return "今日无符合条件的信号"

    # 使用策略执行器生成交易计划
    from trading.strategy_executor import StrategyExecutor

    executor = StrategyExecutor()

    # 将 TechnicalSignal 列表转换为 DataFrame（trade_decision 期望 DataFrame 输入）
    import pandas as pd

    signal_dicts = []
    for s in signals_df:
        signal_dicts.append({
            'code': s.code,
            'name': s.name,
            'signal_type': s.signal_type,
            'score': s.score,
            'current_price': s.current_price,
            'ma5': s.ma5,
            'ma10': s.ma10,
            'ma20': s.ma20,
            'bias_ma5': s.bias_ma5,
            'volume_ratio': s.volume_ratio,
            'turnover_rate': s.turnover_rate,
            'description': s.description,
        })

    signals_dataframe = pd.DataFrame(signal_dicts)
    report = executor.analyze_after_market(signals_dataframe)

    return report


def _get_realtime_data(code: str) -> Optional[Dict]:
    """
    获取单只股票实时行情
    
    Returns:
        dict with: price, volume_ratio, ma5, ma10, change_pct
        失败返回 None
    """
    try:
        import akshare as ak
        
        # 获取实时行情
        df = ak.stock_zh_a_spot_em()
        row = df[df['代码'] == code]
        
        if row.empty:
            # 尝试 6 位代码（带后缀）
            for suffix in ['SH', 'SZ', 'BJ']:
                full_code = f"{code}.{suffix}"
                row = df[df['代码'] == code]
                if not row.empty:
                    break
        
        if row.empty:
            logger.warning(f"获取 {code} 实时行情失败")
            return None
            
        row = row.iloc[0]
        
        # 获取历史均线数据
        ma5, ma10 = None, None
        try:
            hist = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=(datetime.now() - __import__('datetime').timedelta(days=30)).strftime('%Y%m%d'),
                end_date=datetime.now().strftime('%Y%m%d')
            )
            if hist is not None and len(hist) >= 10:
                ma5 = hist['收盘'].iloc[-5:].mean()
                ma10 = hist['收盘'].iloc[-10:].mean()
        except Exception as e:
            logger.debug(f"获取 {code} 历史数据失败: {e}")
        
        change_pct = float(row.get('涨跌幅', 0))
        
        return {
            'price': float(row.get('最新价', 0)),
            'volume_ratio': float(row.get('量比', 1.0)),
            'ma5': ma5,
            'ma10': ma10,
            'change_pct': change_pct,
        }
        
    except Exception as e:
        logger.error(f"获取 {code} 实时数据异常: {e}")
        return None


def execute_trading():
    """
    执行盘中交易
    
    流程：
    1. 加载当日交易计划
    2. 检查持仓止损止盈 → 卖出
    3. 检查买入条件并下单
    
    Returns:
        执行结果文本
    """
    from trading.strategy_executor import StrategyExecutor
    
    now = datetime.now()
    t = now.time()
    
    # 非交易时间不执行
    if not (time(9, 25) <= t <= time(15, 0)):
        logger.info(f"非交易时间({t.strftime('%H:%M')}), 跳过")
        return "非交易时间，跳过执行"
    
    executor = StrategyExecutor()
    plan = executor.load_plan()
    
    if not plan:
        logger.warning("无交易计划")
        return "无交易计划，请先运行 analyze"
    
    logger.info(f"加载交易计划: {plan.get('date')}")
    results = []
    
    # ========== 第一步：持仓止损止盈检查 ==========
    positions = executor.portfolio.get_positions()
    if positions:
        logger.info(f"开始检查 {len(positions)} 个持仓的止损止盈...")
        for pos in positions:
            rt = _get_realtime_data(pos.code)
            
            if rt is None or rt['ma10'] is None:
                logger.warning(f"{pos.name}({pos.code}) 获取实时数据失败，跳过卖出检查")
                continue
            
            sell_result = executor.check_and_sell(
                position_code=pos.code,
                current_price=rt['price'],
                current_ma5=rt['ma5'] if rt['ma5'] else rt['price'],  # 无MA5用当前价兜底
                current_ma10=rt['ma10']
            )
            
            if sell_result:
                action = f"🔴 卖出 {pos.name}({pos.code}): {sell_result}"
                logger.info(action)
                results.append(action)
            else:
                logger.debug(f"{pos.name}({pos.code}) 持仓正常，无需操作")
    else:
        logger.info("当前无持仓")
        results.append("💼 当前空仓，无卖出操作")
    
    # ========== 第二步：买入检查 ==========
    candidates = plan.get('candidates', [])
    if not candidates:
        logger.info("无买入候选")
        results.append("🎯 今日无买入候选")
    else:
        logger.info(f"开始检查 {len(candidates)} 个买入候选...")
        market_status = {
            'index_change': 0,  # TODO: 接入大盘指数涨跌
            'market_open': True,  # 开盘状态
            'is_near_close': t >= time(14, 30),  # 是否临近收盘
        }
        
        for candidate in candidates:
            code = candidate['code']
            name = candidate.get('name', code)
            
            rt = _get_realtime_data(code)
            
            if rt is None:
                logger.warning(f"{name}({code}) 获取实时数据失败，跳过")
                continue
            
            buy_result = executor.execute_buy(
                code=code,
                current_price=rt['price'],
                volume_ratio=rt['volume_ratio'],
                market_status=market_status
            )
            
            if buy_result:
                action = f"🟢 买入 {name}({code}): {buy_result}"
                logger.info(action)
                results.append(action)
            else:
                logger.info(f"{name}({code}) 未满足买入条件，跳过")
    
    # ========== 第三步：收盘前清理（14:50+）==========
    if t >= time(14, 50):
        try:
            executor.cancel_pending_orders()
            results.append("🧹 收盘前委托已清理")
        except Exception as e:
            logger.warning(f"清理委托失败: {e}")
    
    return "\n".join(results) if results else "✅ 所有持仓正常，无买卖操作"


def show_plan():
    """显示当前交易计划"""
    import json
    import os
    
    plan_file = 'trading_plan.json'
    
    if not os.path.exists(plan_file):
        print("暂无交易计划，请先运行 analyze")
        return
    
    with open(plan_file, 'r', encoding='utf-8') as f:
        plan = json.load(f)
    
    print(f"\n{'='*50}")
    print(f"📅 交易日期: {plan['date']}")
    print(f"{'='*50}")
    
    candidates = plan.get('candidates', [])
    holdings = plan.get('holdings', [])
    funds = plan.get('funds', {})
    
    if candidates:
        print(f"\n🎯 买入候选 ({len(candidates)} 只):")
        for s in candidates:
            ma5 = s.get('ma5', '-')
            ma10 = s.get('ma10', '-')
            print(f"   {s['name']}({s['code']}) 评分:{s['score']} MA5:{ma5} MA10:{ma10}")
    else:
        print("\n🎯 买入候选: 无")
    
    if holdings:
        print(f"\n💼 当前持仓 ({len(holdings)} 只):")
        for h in holdings:
            print(f"   {h['name']}({h['code']}) {h['volume']}股")
    else:
        print("\n💼 当前持仓: 空仓")
    
    print(f"\n💰 可用资金: {funds.get('available', '未知')}")


def send_notification(message: str):
    """发送微信通知"""
    try:
        from src.notification import NotificationService

        service = NotificationService()

        # NotificationService.send() 接受 content + 可选参数，不使用 title 参数
        if service.is_available():
            success = service.send(message)
            if success:
                logger.info("微信通知已发送")
            else:
                logger.warning("微信通知发送失败")
        else:
            logger.warning("通知服务未配置，跳过发送")

    except Exception as e:
        logger.error(f"微信通知发送失败: {e}")


def main():
    parser = argparse.ArgumentParser(
        description='模拟盘自动交易助手',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python auto_trader.py analyze      # 盘后分析，生成次日计划
  python auto_trader.py execute      # 盘中执行买卖
  python auto_trader.py plan         # 查看当前计划
  python auto_trader.py run          # 一体化：分析+通知
        """
    )
    
    parser.add_argument(
        'command',
        choices=['analyze', 'execute', 'plan', 'run'],
        help='要执行的命令'
    )
    
    parser.add_argument(
        '--notify', 
        action='store_true',
        default=True,
        help='发送微信通知 (默认: True)'
    )
    
    args = parser.parse_args()
    
    # 初始化环境
    setup_env()
    
    result = ""
    
    if args.command == 'analyze':
        result = run_analysis()
        print(result)
        
    elif args.command == 'execute':
        result = execute_trading()
        print(result)
        
    elif args.command == 'plan':
        show_plan()
        return
        
    elif args.command == 'run':
        result = run_analysis()
        print(result)
    
    # 发送微信通知
    if args.notify and result and args.command in ('analyze', 'run'):
        send_notification(result)


if __name__ == '__main__':
    main()
