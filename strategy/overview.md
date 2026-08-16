# 策略总览

> 本文件是策略体系的唯一资金分配口径：各账户的钱分给哪个策略、分多少。
> 各策略文档只管自己那笔钱怎么用，占比以本文件为准；不用的策略占比即 0，预算归并到对应账户的现金仓。

## 账户与资金分配

| 账户 | 策略 | 资金占比 | 状态 | 策略文档 |
|---|---|---|---|---|
| 主账户 | ETF 长期配置（核心仓） | 90% | 运行中 | [etf_allocation.md](./etf_allocation.md) |
| 主账户 | 量价爆发突破 — ETF 火箭（卫星仓） | 10% 预算 | 已实现待验证 | [rocket_breakout.md](./rocket_breakout.md) |
| 主账户 | 行业轮动 | 0%（观察工具，不交易） | 观察 | [sector_rotation.md](./sector_rotation.md) |
| 子账户 | 趋势交易（趋势回调买入） | 固定 1 万元 | 运行中 | [trend_strategy.md](./trend_strategy.md) |
| 子账户 | 量价爆发突破（红色火箭个股版） | 0%（设计稿，待审批） | 待审批 | [rocket_breakout.md](./rocket_breakout.md) |

> 高股息防御、现金管理、黄金对冲不是独立策略，是 ETF 配置内部的资产类别（红利ETF / CASH / 黄金ETF），见 etf_allocation.md。
