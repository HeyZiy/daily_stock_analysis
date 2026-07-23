# 📈 How will it come?

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
 
---

## 框架

```
                    ┌─────────────────┐
                    │  市场环境门控      │  ← 共享，每日收盘后运行
                    │  market_gate.py  │
                    │  硬拦截 + 4项条件  │
                    └───────┬─────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼                               ▼
    ┌───────────────┐               ┌───────────────┐
    │  ETF 长期配置   │               │  趋势交易策略   │
    │  主账户(90%)   │               │  子账户(1万)   │
    │               │               │               │
    │  gate→偏移→    │               │  gate→系数→    │
    │  再平衡建议     │               │  买卖信号       │
    └───────────────┘               └───────────────┘
            │                               │
            ▼                               ▼
    ┌───────────────────────────────────────────────┐
    │  妙想模拟仓 (mx-moni)                          │
    │  持仓查询 / 市价买卖 / 资金管理                 │
    └───────────────────────────────────────────────┘
            │
            ▼
    ┌───────────────────────────────────────────────┐
    │  通知渠道                                       │
    │  飞书 / 钉钉 / Discord / 邮件 / 企业微信        │
    └───────────────────────────────────────────────┘
```

---

## 策略体系

### ETF 长期配置

压舱石。10 只精选 ETF 覆盖 A 股/海外/债券/黄金/现金，中性基准写死，每日 gate 驱动战术偏移，偏离触发再平衡。

```bash
python etf_allocation.py              # 盘后分析，出再平衡报告
python etf_allocation.py --execute    # 盘中执行，市价调仓
```

📖 策略文档：[docs/etf_allocation.md](docs/etf_allocation.md)

| gate 状态 | 动作 | 权益偏移 |
|---|---|---|
| trending_up | 加仓 | +15% |
| sideways | 维持 | ±0 |
| trending_down | 减仓 | −20% |
| hard_intercept | 清仓 | −40% |

### 趋势交易

1 万练手。均线多头 + 缩量回踩 MA5 买点，趋势破坏即卖。不做加速追高，不做情绪高潮接力。

```bash
python trend_analysis.py              # 日度分析
python trend_analysis.py --debug      # 调试模式
python stock_selector.py "均线多头"   # 选股
```

📖 策略文档：[docs/trend_strategy.md](docs/trend_strategy.md)

| gate 状态 | 系数 | 动作 |
|---|---|---|
| trending_up | ×1.0 | 正常买入 |
| trending_down | ×0.5 | 禁止开仓，收紧止损 |
| chaos | ×0.0 | 空仓 |

---

## 模块清单

```
trend_analysis.py       — 趋势交易策略（信号检测 + 报告 + 通知）
stock_selector.py       — 选股器（妙想 MX API 智能选股）
etf_allocation.py       — ETF 长期配置（再平衡分析 + 执行）

src/
  market_gate.py        — 市场环境门控（硬拦截 + 4 项条件 + 5 级状态）
  mx_client.py          — 妙想模拟组合 API 客户端
  etf_config.py         — ETF 中性基准表 + 战术偏移规则
  etf_rebalancer.py     — ETF 再平衡引擎
  config.py             — 全局配置管理
  notification.py       — 多渠道通知服务

data_provider/          — 多源行情数据（akshare/efinance/tushare/baostock）

docs/
  market.md             — 市场门控设计文档
  trend_strategy.md     — 趋势交易策略文档
  etf_allocation.md     — ETF 配置策略文档
  mx_skills/            — 妙想 Skill API 参考文档
```

---

## 每日执行时序（GitHub Actions）

```
北京时间
15:00  收盘
15:30  ① stock_selector    → 初筛候选股，写妙想自选
16:00  ② trend_analysis    → 趋势策略分析 + 买卖信号报告
16:00  ③ etf_allocation    → ETF 再平衡分析（出计划，暂不执行）
       ─── 次日盘中 ───
 9:35  ④ etf_allocation --execute → 执行昨日再平衡计划（手动/可选）
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
| `SMART_SCREEN_KEYWORD` | 选股条件（留空则每次手动指定） |

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
# 填入 MX_APIKEY 和通知渠道

# === 趋势交易策略 ===
python trend_analysis.py                    # 完整分析
python trend_analysis.py --debug --no-notify  # 调试
python stock_selector.py "均线多头，涨幅2%-7%"  # 选股

# === ETF 长期配置 ===
python etf_allocation.py                    # 盘后分析
python etf_allocation.py --execute          # 盘中执行调仓
```

---

## 市场门控

所有策略共享同一套门控逻辑，见 [docs/market.md](docs/market.md)。

**硬拦截（触发任一直接锁仓+清仓）：**

| 条件 | 阈值 |
|---|---|
| 成交额冰点 | 两市 < 1.5 万亿 连 3 天 |
| 千股跌停 | 跌停 ≥ 50 且 > 涨停 × 3 |
| 指数暴跌 | 上证跌 > 3% |
| 成交量骤降 | 当日 < 20 日均量 × 0.5 |

**4 项门控：**

| # | 条件 | 类型 |
|---|---|---|
| ① | 上证 > MA20 | 趋势 |
| ②a | 两市成交额 ≥ 1.5 万亿 | 量能 |
| ②b | 成交量 > 近 20 日均量 | 量能 |
| ③ | 涨停 ≥ 30 且 > 跌停 × 1.5 | 情绪 |

≥2 项通过 → 开仓。通过后按 5 级市场状态（trending_up / weak_up / sideways / trending_down / chaos）决定评分系数和持仓策略。

---

## 关键环境变量

```bash
# 妙想 API（必填）
MX_APIKEY=your_mx_apikey

# 选股条件
SMART_SCREEN_KEYWORD=均线多头排列，涨幅2%-7%，换手率超过5%

# 通知（至少一个）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
# EMAIL_SENDER=sender@example.com
# EMAIL_PASSWORD=...
# EMAIL_RECEIVERS=receiver@example.com
```

---

> ⚠️ **免责声明**：仅供个人学习研究，不构成投资建议。股市有风险，投资需谨慎。
