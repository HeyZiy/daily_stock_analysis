# AGENTS.md — Regime Trader（全天候策略系统）

## Architecture

```
strategy_planner.py  → 双 Agent 策略规划器：Agent1 市场诊断+从零提议策略+推荐；Agent2 检查推荐策略是否有实现，无则进待办库

src/strategy_planner/           ← 策略规划器（数据采集 + 双 Agent 分析 + 实现检查 + 报告）
src/analysis/market_gate.py     ← 共享门控模块（硬拦截 + 4项条件 + 5级状态）
src/analysis/report.py          ← Markdown 日报生成
src/analysis/strategy/          ← 信号检测(signal_detector.py)、剔除规则(removal_rules.py)
src/etf/                        ← ETF 估值门控、配置、估值因子封装(amazing_factors.py)
src/notify/                     ← 多渠道通知（飞书/钉钉/Discord/邮件）
src/mx/                         ← 妙想模拟仓 API 客户端
data_provider/                  ← 多源行情数据（AmazingData > efinance > akshare > tushare > baostock > yfinance）
```

## Commands

```bash
# 策略规划器
python strategy_planner.py                    # 市场诊断 + 策略适配 + 自进化
python strategy_planner.py --no-llm           # 只看采集的数据，跳过 LLM
python strategy_planner.py --list-strategies  # 查看策略池
python strategy_planner.py --list-pending     # 查看待审批策略
python strategy_planner.py --approve ID       # 批准新策略

# 趋势交易策略
python trend_analysis.py                    # 日度趋势分析（默认先松筛补充自选池再分析）
python trend_analysis.py --no-screen        # 跳过松筛，只分析当前自选池
python trend_analysis.py --screen-keyword "..."  # 自定义松筛条件
python trend_analysis.py --debug --no-notify
python trend_analysis.py --stocks 000001,600519

# ETF 长期配置
python etf_observe.py                    # 周度观察报告
python etf_observe.py --no-notify        # 不发送通知
python etf_observe.py --debug            # 调试模式
```

No test suite, no lint/typecheck commands.

## Config

- `.env` at project root, loaded via `python-dotenv` in `src/config.py:setup_env()`
- `setup_env()` must be called before importing config-dependent modules
- Required: `MX_APIKEY`
- `Config` is a singleton dataclass accessed via `Config.get_instance()`
- LLM 需要 `DEEPSEEK_API_KEY` 或 `LLM_CHANNELS`

## CI

- GitHub Actions, Python 3.11 on `ubuntu-latest`
- `trend_analysis.yml` runs weekdays at 13:30 Beijing time (UTC 05:30)
- `etf_weekly.yml` runs Saturdays at 09:00 Beijing time (UTC 01:00)
- `strategy_planner.yml` runs Saturdays at 09:00 Beijing time (UTC 01:00)
- Auto-tag on `main` push commits containing `#patch`, `#minor`, or `#major`
- Release auto-created on `v*.*.*` annotated tag push

## Design Decisions

- **文档分工**：
  - `strategy/*.md` — 项目自身的策略设计文档（门控/趋势/ETF 配置），**必须与代码同步**。改代码中的阈值、交易逻辑、信号优先级时，必须在同一提交里更新对应策略文档；反之改文档时也要同步代码。
  - `docs/*.md`（含 mx_skills/、星耀数智/）— 外部工具说明书（妙想 API、AmazingData SDK），仅作参考，无需与代码同步。
- 当文档与代码出现矛盾、或需要决策阈值/逻辑时，**以投资/交易逻辑为准**思考什么对策略合理，而不是"文档说了什么"或"代码现在怎么写的"。
- 策略池是自进化的：`data/strategy_registry.json` 持久化所有策略，LLM 可提议新策略加入待审批区。
- 策略待办库：`data/strategy_todo.json` — Agent 2 发现推荐策略无实现时登记，含 doc_ref 指向 strategy/*.md。

## Key Conventions

- Chinese docstrings and comments throughout
- `data/` holds cached state (e.g. `market_gate_ice_days.json`, `strategy_registry.json`)
- Logging via `src/logging_config.py:setup_logging()` — console + file + debug file handlers
- All stock codes normalized via `data_provider.base:canonical_stock_code()`
