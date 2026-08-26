# Regime Trader — A股智能交易系统

个人 A 股量化交易系统：趋势波段、ETF 长期配置、行业轮动观察 + LLM 策略规划器（自进化）。

- **数据**：个股多源行情（AmazingData > tushare > akshare > efinance > baostock > yfinance，自动降级容错）；ETF / A股指数走 akshare 单源（`data_provider/bars.py`）
- **交易**：妙想模拟仓 API（`src/mx/`）
- **通知**：飞书 / 钉钉 / 企业微信 / 邮件多渠道推送
- **部署**：Linux 云服务器 + crontab（已从 GitHub Actions 迁移）

## 策略体系

资金分配口径见 [strategy/overview.md](strategy/overview.md)，各策略文档见 [strategy/](strategy/)：

| 账户 | 策略 | 状态 | 文档 |
|---|---|---|---|
| 主账户 | ETF 长期配置（核心仓） | 运行中 | [etf_allocation.md](strategy/etf_allocation.md) |
| 主账户 | 量价爆发突破 — ETF 火箭（卫星仓） | 已实现待验证 | [rocket_breakout.md](strategy/rocket_breakout.md) |
| 主账户 | 行业轮动 | 观察工具，不交易 | [sector_rotation.md](strategy/sector_rotation.md) |
| 子账户 | 趋势交易（趋势回调买入） | 运行中 | [trend_strategy.md](strategy/trend_strategy.md) |
| 子账户 | 量价爆发突破（个股版） | 待审批 | [rocket_breakout.md](strategy/rocket_breakout.md) |

> 高股息防御、现金管理、黄金对冲是 ETF 配置内部的资产类别（红利ETF / CASH / 黄金ETF）。

## 三个入口

| 脚本 | 定位 | 频率 |
|---|---|---|
| `trend_analysis.py` | 趋势交易：市场门控 + 分歧回踩买点检测 + 观察池维护 + 次日交易计划 | 每交易日 13:30 |
| `etf_observe.py` | ETF 周度观察 + `--execute` 统一调仓（核心再平衡 + 卫星火箭） | 每周一 9:35 |
| `strategy_planner.py` | 双 Agent 策略规划器：市场诊断 → 策略适配推荐 → 实现检查 → 自进化提议 | 每周六 9:00 |

## 快速开始

```bash
# 1. 环境（本地 Windows 开发 / 服务器 Linux 均可）
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. 私有数据源（可选，需账号凭证）
pip install wheels/AmazingData-1.1.9-cp314-none-any.whl wheels/tgw-1.0.9.2-py3-none-any.whl

# 3. 创建 .env 并填写：妙想 API、LLM、通知渠道等（见下表）

# 4. 运行
python trend_analysis.py          # 趋势日度分析
python etf_observe.py             # ETF 周度观察（--execute 才下单）
python style_report.py            # 风格状态周报（纯规则，无 LLM）
python strategy_planner.py        # 策略规划（需 LLM）
```

主要环境变量（`.env`）：

| 变量 | 说明 |
|---|---|
| `MX_APIKEY` | 妙想模拟仓 API Key |
| `TGW_*` | AmazingData / TGW 行情服务登录凭证 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `LITELLM_MODEL` | 策略规划器 LLM |
| `FEISHU_WEBHOOK_URL` | 飞书群机器人通知（推荐） |
| `CUSTOM_WEBHOOK_URLS` / `WECHAT_WEBHOOK_URL` | 钉钉 / 企业微信机器人（选填） |
| `EMAIL_*` | 邮件通知（选填） |
| `TUSHARE_TOKEN` | Tushare Pro Token（数据源降级备用） |

## 常用命令

```bash
# 趋势交易
python trend_analysis.py --no-screen              # 跳过松筛，只分析当前自选池
python trend_analysis.py --screen-keyword "..."   # 自定义松筛条件
python trend_analysis.py --stocks 000001,600519  # 指定股票
python trend_analysis.py --trade                  # 盘后生成次日交易计划
python trend_analysis.py --trade-execute          # 盘中执行止损止盈/买入
python trend_analysis.py --trade-plan             # 查看当前交易计划

# ETF 配置
python etf_observe.py --execute                   # 执行统一调仓批次（妙想市价单）
python etf_observe.py --force                     # 跳过交易日检查（调试）

# 策略规划器
python strategy_planner.py --list-strategies      # 查看策略池
python strategy_planner.py --list-pending         # 查看待审批策略
python strategy_planner.py --approve ID           # 批准新策略（--remove ID 移除）

# 通用
python xxx.py --debug --no-notify                 # 调试模式 + 不推送通知
```

## 目录结构

```
strategy_planner.py      双 Agent 策略规划器入口（诊断 + 推荐 + 自进化）
trend_analysis.py        趋势交易日度分析入口
etf_observe.py           ETF 周度观察/调仓入口
data_provider/           数据接入层
  ├ bars.py              日线入口：get_etf_daily(ETF) / get_index_daily(A股指数)，akshare 单源
  ├ manager.py           个股日线多源（AmazingData>Tushare>…）+ get_fetcher
  ├ realtime.py          实时报价跨源合并 merge_realtime_quotes
  ├ codes.py / types.py  契约：代码判定 / 统一类型（STANDARD_COLUMNS + 异常 + 实时类型）
  └ fetchers/            数据源实现（base + akshare / efinance / tushare / yfinance / baostock / amazingdata）
src/analysis/            市场门控 market_gate、报告生成 report、信号检测 signal_detector、剔除规则 removal_rules
src/etf/                 ETF 配置：估值门控、再平衡、火箭引擎、行业轮动、因子封装、基准配置
src/mx/                  妙想模拟仓 API 客户端
src/notify/              多渠道通知（飞书/钉钉/企业微信/邮件）
src/strategy_planner/    策略规划器（数据采集 + 双 Agent + 实现检查）
strategy/                策略设计文档（与代码同步维护）
data/                    策略池 registry / 待办库 todo / 运行状态缓存
deploy/crontab.server    服务器定时任务配置
docs/                    外部工具说明书
```

## 部署（云服务器）

```bash
# 前置（详见 deploy/crontab.server 顶部注释）
cd /srv/regime-trader
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# 配置 .env（含 MX_APIKEY / TGW_* 等），确认时区为 Asia/Shanghai

# 安装定时任务
crontab deploy/crontab.server
```

定时任务一览（`flock` 防重入，节假日由 `src/trading_calendar.py` 或 cron 的 `1-5` 处理）：

| 时间 | 任务 |
|---|---|
| 每交易日 13:30 | 趋势跟踪分析 |
| 每周一 9:35 | ETF 周度观察 + 自动调仓 |
| 每周六 9:00 | 策略规划器 |

## 策略自进化

`strategy_planner.py` 的 Agent2 检查推荐策略是否已有实现，无实现则登记到 `data/strategy_todo.json`；新策略写入 `data/strategy_registry.json` 待审批区，用 `--approve ID` 转正后 Agent1 才会推荐使用。

## 开发约定

- 中文 docstring 与注释；日志经 `src/logging_config.py`（console + file + debug file）
- 股票代码统一走 `data_provider/base.py:canonical_stock_code()`
- `strategy/*.md` 与代码**必须同步维护**：改阈值/交易逻辑/信号优先级时，同一提交内更新对应策略文档
- 无测试套件；无 lint/typecheck 命令


> ⚠️ **免责声明**：仅供个人学习研究，不构成投资建议。股市有风险，投资需谨慎。