# -*- coding: utf-8 -*-
"""
每日决策辅助脚本 - 交易手册执行清单

用法:
    python daily_decision.py              # 显示今日决策流程
    python daily_decision.py --plan        # 查看交易计划
    python daily_decision.py --check       # 检查买入条件
    python daily_decision.py --position    # 检查持仓止损止盈
"""

import argparse
import json
import logging
import os
from datetime import datetime, time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ==================== 策略常量 ====================

STRATEGY_CONFIG = {
    # 买入条件
    'buy_score_min': 80,          # 最低评分
    'volume_ratio_max': 1.0,      # 最大量比
    'change_pct_max': 3.0,        # 最大涨跌幅%
    
    # 时间窗口
    'aggressive_buy_start': '09:40',
    'aggressive_buy_end': '10:00',
    'safe_buy_start': '14:30',
    'safe_buy_end': '15:00',
    
    # 止损止盈
    'stop_loss_ma10': True,       # 跌破MA10止损
    'take_profit_1_low': 3.0,     # 第一止盈下限%
    'take_profit_1_high': 5.0,    # 第一止盈上限%
    'take_profit_2': 7.0,         # 第二止盈%
    
    # 风险控制
    'max_single_position': 0.30,  # 单票最大仓位%
    'max_holdings': 2,           # 最大持仓数
    'market_risk_threshold': -2.0, # 大盘风险阈值%
}


def show_checklist():
    """显示每日决策检查清单"""
    print("""
╔══════════════════════════════════════════════════╗
║         📋 每日交易决策检查清单                    ║
╚══════════════════════════════════════════════════╝

🕐 当前时间: {}

【开盘前准备】(9:15-9:25)
□ 1. 打开昨日盘后报告
□ 2. 只关注 🎯 缩量回踩买点 (评分≥80分)
□ 3. 记录信号股的 MA5、MA10、当前价
□ 4. 查看大盘/创业板涨跌情况

【盘中执行】(9:30-15:00)

  ⏰ 9:40-10:00  最佳买入窗口（激进）
  □ 验证买入条件：
     □ 不跌破昨日 MA10 ✓/✗
     □ 开盘量比 ≤ 1.0 ✓/✗  
     □ 日内涨跌幅 ±3%以内 ✓/✗
  □ 如满足 → 在 MA10~MA5 区间买入
  
  ⏰ 14:30-15:00  尾盘买入窗口（稳健）
  □ 同上验证，收盘前确认

  ⏰ 全天监控
  □ 持仓止损检查：
     □ 是否有效跌破 MA10？
     □ 是 → 止损卖出，同步剔除
  □ 持仓止盈检查：
     □ 盈利 3%-5% → 减仓50%
     □ 盈利 7%+   → 清仓
     □ 跌破 MA5   → 减仓50%

【放弃买入触发】(任一即放弃)
□ 大盘/创业板大跌超 2%
□ 个股突发利空
□ 开盘放量跌破 MA10，10分钟收不回
□ 次日高开超 5%

【收盘后】(15:05+)
□ 运行 main_simple.py 生成新报告
□ 更新自选股列表
□ 复盘当日操作

""".format(datetime.now().strftime('%H:%M')))


def show_plan():
    """显示当前交易计划"""
    plan_file = 'trading_plan.json'
    
    if not os.path.exists(plan_file):
        print("\n⚠️  暂无交易计划")
        print("请先运行: python trading/auto_trader.py analyze\n")
        return
    
    with open(plan_file, 'r', encoding='utf-8') as f:
        plan = json.load(f)
    
    candidates = plan.get('candidates', [])
    holdings = plan.get('holdings', [])
    
    print(f"\n{'='*55}")
    print(f"📅 交易日: {plan['date']}")
    print(f"{'='*55}")
    
    if candidates:
        print(f"\n🎯 今日买入候选 ({len(candidates)} 只):\n")
        for i, s in enumerate(candidates, 1):
            ma5 = s.get('ma5', '?')
            ma10 = s.get('ma10', '?')
            price = s.get('current_price', '?')
            
            print(f"  {i}. {s['name']}({s['code']})")
            print(f"     评分: {s['score']} | 价格: {price}")
            print(f"     MA5: {ma5} | MA10: {ma10}")
            print(f"     买入区间: [{ma10} ~ {ma5}]")
            print()
        
        print("  ⚠️  买入条件:")
        print("     ① 不跌破 MA10 (趋势生命线)")
        print("     ② 量比 ≤ 1.0 (缩量洗盘)")
        print("     ③ 涨跌幅 ±3% 以内")
        print()
        print("  ⏰ 最佳时间:")
        print("     激进: 9:40-10:00 | 稳健: 14:30-15:00")
        print()
    else:
        print("\n🎯 今日无符合条件的买入候选\n")
    
    if holdings:
        print(f"💼 当前持仓 ({len(holdings)} 只):\n")
        for h in holdings:
            print(f"  • {h['name']}({h['code']}) {h['volume']}股")
        print()
    
    print("-"*55)


def check_buy_conditions():
    """
    交互式买入条件检查
    用户输入实时数据，判断是否可以买入
    """
    print("\n🔍 买入条件检查器")
    print("-"*40)
    
    try:
        code = input("股票代码: ").strip()
        name = input("股票名称: ").strip()
        current_price = float(input("当前价格: "))
        ma10 = float(input("昨日 MA10: "))
        ma5 = float(input("昨日 MA5: "))
        volume_ratio = float(input("当前量比: "))
        change_pct = float(input("日内涨跌幅(%): ") or "0")
        
        print()
        print("=" * 45)
        print("📊 条件检查结果")
        print("=" * 45)
        
        passed = True
        
        # 条件1：不跌破MA10
        cond1_ok = current_price >= ma10 * 0.995
        status1 = "✅ 通过" if cond1_ok else "❌ 未通过"
        print(f"\n① 不跌破 MA10 ({ma10:.2f}): {status1}")
        if not cond1_ok:
            print(f"   → 当前价 {current_price:.2f} < MA10 {ma10:.2f}")
            passed = False
        
        # 条件2：量比≤1.0
        cond2_ok = volume_ratio <= STRATEGY_CONFIG['volume_ratio_max']
        status2 = "✅ 通过" if cond2_ok else "❌ 未通过"
        print(f"② 量比 ≤ 1.0: {status2} (实际:{volume_ratio:.2f})")
        if not cond2_ok:
            print("   → 放量，非缩量洗盘形态")
            passed = False
        
        # 条件3：涨跌幅±3%
        cond3_ok = abs(change_pct) <= STRATEGY_CONFIG['change_pct_max']
        status3 = "✅ 通过" if cond3_ok else "❌ 未通过"
        print(f"③ 涨跌幅 ±3%: {status3} (实际:{change_pct:+.2f}%)")
        if not cond3_ok:
            print("   → 波动过大，不符合小实体洗盘")
            passed = False
        
        # 买入区间
        buy_lower = ma10 * 0.998
        buy_upper = ma5 * 1.002
        in_zone = buy_lower <= current_price <= buy_upper
        zone_status = "✅ 在区间内" if in_zone else "⚠️ 不在最佳区间"
        print(f"\n④ 买入区间 [{buy_lower:.2f} ~ {buy_upper:.2f}]: {zone_status}")
        
        # 时间窗口
        now = time(*map(int, datetime.now().strftime("%H:%M").split(":")))
        t_aggressive_start = time(*map(int, STRATEGY_CONFIG['aggressive_buy_start'].split(":")))
        t_aggressive_end = time(*map(int, STRATEGY_CONFIG['aggressive_buy_end'].split(":")))
        t_safe_start = time(*map(int, STRATEGY_CONFIG['safe_buy_start'].split(":")))
        
        if t_aggressive_start <= now <= t_aggressive_end:
            time_msg = "⏰ 激进买入窗口 (9:40-10:00)"
        elif t_safe_start <= now <= time(15, 0):
            time_msg = "⏰ 稳健买入窗口 (14:30-15:00)"
        else:
            time_msg = "⏰ 非最佳买入时段"
        print(f"⑤ 时间窗口: {time_msg}")
        
        # 最终结论
        print("\n" + "=" * 45)
        if passed and in_zone:
            print(f"🟢 结论: 可以买入 {name}({code})")
            print(f"   建议价格: {max(buy_lower, current_price):.2f}")
            print(f"   止损位: MA10 = {ma10:.2f}")
        else:
            reasons = []
            if not cond1_ok:
                reasons.append("跌破MA10生命线")
            if not cond2_ok:
                reasons.append("放量(非缩量)")
            if not cond3_ok:
                reasons.append("波动过大")
            
            reason_str = "、".join(reasons) if reasons else "不在买入区间"
            print(f"🔴 结论: 建议暂不买入")
            print(f"   原因: {reason_str}")
        print("=" * 45 + "\n")
        
    except ValueError as e:
        print(f"\n输入格式错误: {e}\n")


def check_position_stop_loss():
    """
    持仓止损止盈检查
    """
    print("\n💼 持仓止损止盈检查")
    print("-"*40)
    
    try:
        code = input("股票代码: ").strip()
        cost_price = float(input("成本价: "))
        current_price = float(input("当前价格: "))
        ma10 = float(input("当前 MA10: "))
        ma5 = float(input("当前 MA5: "))
        
        profit_pct = (current_price - cost_price) / cost_price * 100
        
        print()
        print("=" * 45)
        print("📊 止损止盈分析")
        print("=" * 45)
        
        actions = []
        
        # 止损检查
        if current_price < ma10 * 0.995:
            actions.append({
                'type': '🔴 止损',
                'action': '全部卖出',
                'reason': f'跌破MA10 ({current_price:.2f} < {ma10:.2f})',
                'ratio': 1.0
            })
        
        # 止盈检查
        if STRATEGY_CONFIG['take_profit_1_low'] <= profit_pct < STRATEGY_CONFIG['take_profit_1_high']:
            actions.append({
                'type': '🟢 第一止盈',
                'action': '减仓50%',
                'reason': f'盈利{profit_pct:.1f}% (3-5%区间)',
                'ratio': 0.5
            })
        elif profit_pct >= STRATEGY_CONFIG['take_profit_2']:
            actions.append({
                'type': '🟢 第二止盈',
                'action': '清仓',
                'reason': f'盈利{profit_pct:.1f}% (≥7%)',
                'ratio': 1.0
            })
        
        # MA5检查
        if current_price < ma5 * 0.998 and not any(a['type'] == '🔴 止损' for a in actions):
            actions.append({
                'type': '🟡 趋势走弱',
                'action': '减仓50%',
                'reason': f'跌破MA5 ({current_price:.2f} < {ma5:.2f})',
                'ratio': 0.5
            })
        
        # 输出结果
        print(f"\n持仓信息: 成本={cost_price:.2f} 现价={current_price:.2f}")
        print(f"盈亏: {profit_pct:+.2f}%\n")
        
        if actions:
            for a in actions:
                print(f"{a['type']}: {a['action']}")
                print(f"   原因: {a['reason']}")
                print()
        else:
            print("✅ 暂无需操作，继续持有")
            print(f"   当前盈利: {profit_pct:+.2f}%")
            print(f"   止损位: MA10={ma10:.2f}")
            print(f"   第一止盈目标: +{STRATEGY_CONFIG['take_profit_1_low']}%~+{STRATEGY_CONFIG['take_profit_1_high']}%")
            print()
        
        print("=" * 45 + "\n")
        
    except ValueError as e:
        print(f"\n输入格式错误: {e}\n")


def show_strategy_summary():
    """显示策略要点速查"""
    print("""

╔═════════════════════════════════════════════════════╗
║           📖 技术分析策略速查卡                       ║
╚═════════════════════════════════════════════════════╝

【核心信号】只做 🎯 缩量回踩MA5 (评分≥80)

【买入三条件】缺一不可
  ✅ 不跌破 MA10 (趋势生命线)
  ✅ 量比 ≤ 1.0 (延续缩量)
  ✅ 涨跌幅 ±3% (小实体洗盘)

【买入时间】
  激进: 9:40-10:00 (成本最优)
  稳健: 14:30-15:00 (风险最低)
  ❌ 禁止: 9:25-9:30 (集合竞价)

【买入价位】
  区间: 昨日 MA10 ~ 昨日 MA5
  例: MA10=31.18, MA5=31.61 → [31.10 ~ 31.60]

【止损规则】无条件执行
  触发: 有效跌破 MA10 (收盘前不收回)
  动作: 全部卖出，同步剔除该票

【止盈规则】分批卖出
  +3%~+5% : 减仓 50%
  +7%以上 : 清仓
  不破MA5 : 继续持有

【仓位控制】
  单票: 总资金 20%-30%
  同时持仓: ≤ 2 只
  大盘弱势: 仓位减半

【放弃条件】任一触发即放弃
  • 大盘/创业板大跌 > 2%
  • 个股突发利空
  • 放量跌破MA10，10分钟不收回
  • 高开 > 5%

""")


def main():
    parser = argparse.ArgumentParser(
        description='每日交易决策辅助工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python daily_decision.py               # 显示决策流程
  python daily_decision.py --plan        # 查看交易计划
  python daily_decision.py --check       # 检查买入条件
  python daily_decision.py --position    # 检查止损止盈
  python daily_decision.py --strategy    # 策略速查卡
        """
    )
    
    parser.add_argument('--plan', action='store_true', help='查看交易计划')
    parser.add_argument('--check', action='store_true', help='买入条件检查器')
    parser.add_argument('--position', action='store_true', help='持仓止损止盈检查')
    parser.add_argument('--strategy', action='store_true', help='策略要点速查')
    
    args = parser.parse_args()
    
    if args.strategy:
        show_strategy_summary()
    elif args.plan:
        show_plan()
    elif args.check:
        check_buy_conditions()
    elif args.position:
        check_position_stop_loss()
    else:
        show_checklist()


if __name__ == '__main__':
    main()
