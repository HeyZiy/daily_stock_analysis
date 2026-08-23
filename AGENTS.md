# AGENTS.md — Regime Trader

## Architecture

```
strategy_planner.py  → 双 Agent 策略规划器【已停用 2026-08-23，代码留档】：Agent1 市场诊断+从零提议策略；
                       Agent2 LLM 对账策略池注册表（已实现/未实现/全新），无实现进待办库
style_report.py     → 风格状态周报（纯规则，无 LLM）：周度判定 主线强势期/退潮期/真空期/形成中 + 主导风格，
                       落盘 data/style_state.json；--backtest 历史回放验证标签

src/strategy_planner/           ← 策略规划器（已停用；数据采集 + 双 Agent 分析 + 实现检查 + 报告）
src/analysis/market_gate.py     ← 趋势策略市场门控（硬拦截 + 4项条件 + 5级状态）
src/analysis/style_state.py     ← 风格状态判定（取数 + 指标 + 状态机 + 周报生成）
src/analysis/report.py          ← Markdown 日报生成
src/analysis/strategy/          ← 信号检测(signal_detector.py)、剔除规则(removal_rules.py)、
                                   卖出信号(sell_rules.py，读妙想持仓输出减仓/清仓建议)
src/etf/                        ← ETF 配置：估值门控(allocation_gate)、再平衡(rebalancer)、
                                   卫星仓火箭引擎(rocket_breakout)、行业轮动观察(sector_rotation)、
                                   因子封装(amazing_factors)、基准(config)
src/notify/                     ← 多渠道通知（飞书/邮件）
src/mx/                         ← 妙想模拟仓 API 客户端 + 持仓公共工具(position_utils)
data_provider/                  ← 多源行情数据（AmazingData > tushare > akshare > efinance > baostock > yfinance）

data/strategy_registry.json     ← 策略池（LLM 可提议新策略进待审批区，--approve 转正）
data/strategy_todo.json         ← 策略待办库（Agent2 发现推荐策略无实现时登记，含 doc_ref）
data/etf_industry_map.json      ← 行业 ETF 清单（申万行业 → 首选/备选 ETF，卫星仓/轮动引擎标的池）
```

## Commands

```bash
# 策略规划器【已停用，代码留档】
python strategy_planner.py --list-strategies  # 查看策略池
python strategy_planner.py --list-pending     # 查看待审批策略
python strategy_planner.py --approve ID       # 批准新策略（--remove ID 移除）

# 风格状态周报（纯规则，无 LLM；cron 暂未启用）
python style_report.py                    # 周报：风格状态 + 主线明细 + 风格指标 + 门控环境
python style_report.py --backtest 2021-01-01  # 历史回放状态时间线（不落盘不通知）

# 趋势交易（每交易日 15:10 收盘后）
python trend_analysis.py                    # 日度分析（买入信号 + 持仓卖出信号；先松筛补充自选池再分析）
python trend_analysis.py --no-screen        # 跳过松筛，只分析当前自选池
python trend_analysis.py --screen-keyword "..."  # 自定义松筛条件
python trend_analysis.py --stocks 000001,600519  # 指定股票（覆盖妙想自选股）
python trend_analysis.py --list             # 仅列出自选池
python trend_analysis.py --debug --no-notify

# ETF 长期配置（每周一 9:35）
python etf_observe.py                    # 周度观察报告（只出建议，不下单）
python etf_observe.py --execute          # 执行统一批次：核心再平衡 + 卫星火箭调仓（妙想市价单）
python etf_observe.py --force            # 跳过交易日检查（调试用）
python etf_observe.py --no-notify --debug

# 行业轮动观察（引擎未接入交易，只输出排名）
python -m src.etf.sector_rotation
```

No test suite, no lint/typecheck commands.

## 部署与定时任务

- 已从 GitHub Actions 迁移到云服务器（Linux），Python 环境用项目内 `.conda/`
- 定时任务：`deploy/crontab.server`（crontab 格式，服务器时区须为 Asia/Shanghai）
- 每个任务用 `flock` 防重入；节假日由 `src/trading_calendar.py:is_trading_day()` 处理（etf 任务）或 cron 的 `1-5` 限定（trend 任务）

## Design Decisions

- **文档分工**：
  - `strategy/*.md` — 项目自身的策略设计文档（总览/趋势/ETF 配置/行业轮动/红火箭），**必须与代码同步**。改代码中的阈值、交易逻辑、信号优先级时，必须在同一提交里更新对应策略文档；反之改文档时也要同步代码。 
    - `strategy/overview.md`：总览
- 当文档与代码出现矛盾、或需要决策阈值/逻辑时，**以投资/交易逻辑为准**思考什么对策略合理，而不是"文档说了什么"或"代码现在怎么写的"。

## Key Conventions

- Chinese docstrings and comments throughout
- `data/` holds cached state (e.g. `market_gate_ice_days.json`, `rotation_state.json`)
- Logging via `src/logging_config.py:setup_logging()` — console + file + debug file handlers
- All stock codes normalized via `data_provider.base:canonical_stock_code()`

## docs 外部工具说明书

- mx_skills
- 星耀数智