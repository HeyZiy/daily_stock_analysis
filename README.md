# 📊 Regime Trader — 全天候策略系统

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)

一个自进化的全天候量化策略系统：根据市场状态自动规划策略配置，覆盖趋势跟踪、ETF资产配置、行业轮动等多策略。

---

## 框架

```
                    ┌─────────────────────┐
                    │    策略规划器          │  ← 每周末运行
                    │  strategy_planner.py │     市场诊断 + 策略适配
                    │  LLM 驱动 + 自进化     │     策略权重分配 + 进化
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    │   市场环境门控        │  ← 共享，每日收盘后运行
                    │   market_gate.py     │
                    │   硬拦截 + 4项条件     │
                    └──────────┬──────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                                     ▼
    ┌───────────────┐                     ┌───────────────┐
    │  ETF 长期配置   │                     │  趋势交易策略   │
    │  压舱石        │                     │  练手          │
    │               │                     │               │
    │  估值门控 →    │                     │  信号检测 →    │
    │  再平衡执行     │                     │  买卖报告       │
    └───────────────┘                     └───────────────┘
            │                                     │
            ▼                                     ▼
    ┌─────────────────────────────────────────────────────┐
    │  妙想模拟仓 (mx-moni)                                │
    │  持仓查询 / 市价买卖 / 资金管理                       │
    └─────────────────────────────────────────────────────┘
            │
            ▼
    ┌─────────────────────────────────────────────────────┐
    │  通知渠道                                             │
    │  飞书 / 钉钉 / Discord / 邮件 / 企业微信              │
    └─────────────────────────────────────────────────────┘
```

---

## 策略体系

### 策略规划器（自进化）

每周末由 LLM 诊断市场阶段，对策略池中所有策略进行适配度打分和权重分配。LLM 还能主动提议新策略，经审批后加入策略池。

```bash
python strategy_planner.py              # 市场诊断 + 策略适配 + 进化建议
python strategy_planner.py --no-llm     # 仅采集数据，跳过 LLM
python strategy_planner.py --list-strategies  # 查看策略池
python strategy_planner.py --list-pending    # 查看 LLM 提议的新策略
python strategy_planner.py --approve ID      # 批准新策略
```

当前策略池内置 9 个策略：趋势回调买入、ETF PE估值配置、行业轮动、高股息防御、现金管理、网格波段、动量追涨、超跌反弹、黄金商品对冲。策略池持久化在 `data/strategy_registry.json`，随 LLM 提议自动扩展。

### ETF 长期配置

压舱石。10 只精选 ETF 覆盖 A 股/海外/债券/黄金/现金，中性基准写死，每日 gate 驱动战术偏移，偏离触发再平衡。

```bash
python etf_allocation.py              # 盘后分析，出再平衡报告
python etf_allocation.py --execute    # 盘中执行，市价调仓
```

📖 策略文档：[strategy/etf_allocation.md](strategy/etf_allocation.md)

| gate 状态 | 动作 | 权益偏移 |
|---|---|---|
| trending_up | 加仓 | +15% |
| sideways | 维持 | ±0 |
| trending_down | 减仓 | −20% |
| hard_intercept | 清仓 | −40% |

### 趋势交易

均线多头 + 缩量回踩 MA5 买点，趋势破坏即卖。不做加速追高，不做情绪高潮接力。

```bash
python trend_analysis.py              # 日度分析（含松筛选股）
python trend_analysis.py --debug      # 调试模式
python trend_analysis.py --list       # 列出自选池
python trend_analysis.py --screen-keyword "均线多头"  # 自定义选股
```

📖 策略文档：[strategy/trend_strategy.md](strategy/trend_strategy.md)

| gate 状态 | 系数 | 动作 |
|---|---|---|
| trending_up | ×1.0 | 正常买入 |
| trending_down | ×0.5 | 禁止开仓，收紧止损 |
| chaos | ×0.0 | 空仓 |

---

## 每日执行时序（GitHub Actions）

```
北京时间
周一~周五 13:30  ① trend_analysis    → 趋势信号检测 + 买卖建议报告
               ② etf_allocation    → ETF 再平衡分析（出计划）
周六 09:00      ③ strategy_planner  → 市场诊断 + 策略适配 + 策略进化
```

---

## 快速开始（GitHub Actions）

### 1. Fork 本仓库

### 2. 配置 Secrets

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

**必填：**

| Secret | 说明 |
|---|---|
| `MX_APIKEY` | 妙想 API Key（选股 + 模拟仓交易） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（策略规划器 LLM 分析） |

**通知渠道（至少配一个）：**

| Secret | 说明 |
|---|---|
| `FEISHU_WEBHOOK_URL` | 飞书群机器人 |
| `DINGTALK_WEBHOOK_URL` | 钉钉群机器人 |
| `DISCORD_WEBHOOK_URL` | Discord Webhook |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECEIVERS` | 邮件 |

**可选：**

| Secret | 说明 |
|---|---|
| `TUSHARE_TOKEN` | Tushare 数据源 |
| `SMART_SCREEN_KEYWORD` | 趋势选股条件 |

### 3. 启用 Actions

`Actions` → 选择 workflow → 启用

---

## 本地运行

```bash
git clone <your-fork-url>
cd trend-sniper

pip install -r requirements.txt

# 配置 .env
cp .env.example .env
# 填入 MX_APIKEY、DEEPSEEK_API_KEY 和通知渠道

# === 策略规划器 ===
python strategy_planner.py                    # 市场诊断 + 策略适配
python strategy_planner.py --no-llm           # 只看采集的数据
python strategy_planner.py --list-strategies  # 查看策略池

# === 趋势交易策略 ===
python trend_analysis.py                    # 完整分析（含松筛选股）
python trend_analysis.py --debug --no-notify  # 调试
python trend_analysis.py --list             # 列出自选池

# === ETF 长期配置 ===
python etf_allocation.py                    # 盘后分析
python etf_allocation.py --execute          # 盘中执行调仓
```

---

## 市场门控

所有策略共享同一套门控逻辑，见 [strategy/market.md](strategy/market.md)。

**硬拦截（触发任一直接锁仓+清仓）：**

| 条件 | 阈值 |
|---|---|
| 成交额冰点 | 两市 < 1.5 万亿 连 3 天 |
| 千股跌停 | 跌停 ≥ 50 且 > 涨停 × 3 |
| 指数暴跌 | 上证跌 > 3% |
| 成交量骤降 | 当日 < 20 日均量 × 0.5 |

**4 项门控（≥2 项通过才开仓，trending_down 需 ≥3 项）：**

| # | 条件 | 类型 |
|---|---|---|
| ① | 上证 > MA20 | 趋势 |
| ②a | 两市成交额 ≥ 1.5 万亿 | 量能 |
| ②b | 成交量 > 近 20 日均量 | 量能 |
| ③ | 涨停 ≥ 30 且 > 跌停 × 1.5 | 情绪 |

---

## 关键环境变量

```bash
# 妙想 API（必填）
MX_APIKEY=your_mx_apikey

# LLM（策略规划器必填）
DEEPSEEK_API_KEY=your_deepseek_key
# 或 LLM_CHANNELS=deepseek 配合 LLM_DEEPSEEK_API_KEY

# 选股条件（松筛）
SMART_SCREEN_KEYWORD=市值大于30亿小于500亿；均线多头排列；换手率大于3%；不要科创板不要创业板不要北交所不要ST

# 通知（至少一个）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 日志告警：WARNING 及以上日志推送到通知渠道（默认 WARNING；设为 OFF 禁用）
LOG_ALERT_LEVEL=WARNING
```

---

> ⚠️ **免责声明**：仅供个人学习研究，不构成投资建议。股市有风险，投资需谨慎。
