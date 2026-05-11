# 📈 趋势波段自动化系统

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📊 基于博弈仓策略的 A 股趋势波段跟踪系统
>
> 纯技术分析，无 LLM，每日收盘后自动选股、筛信号、推报告。

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **策略选股** | 妙想 API 自然语言选股，每日收盘后初筛候选股 |
| **观察池维护** | 自动追踪自选股，趋势破坏自动剔除（跌破 MA10 / 放量长阴） |
| **市场门控** | 收盘后检查5项环境条件，不满足2条则不建议开仓 |
| **买点检测** | 纯技术分析：均线多头 + 缩量回踩 MA5 分歧信号 |
| **卖出提示** | 分级卖出：放量跌破 MA5 减仓 50%，跌破 MA10 清仓 |
| **大盘复盘** | 独立大盘复盘（可选 LLM 增强，不配置则自动降级） |
| **多渠道通知** | 飞书 / 钉钉 / Discord / 邮件，任配一个即可 |
| **零成本运行** | GitHub Actions 免费计算，无需服务器 |

---

## 🗂 核心模块

```
mx_smart_screen.py   — 每日初筛选股（调用妙想 MX API）
main.py       — 策略主控（观察池 + 买卖信号 + 报告 + 通知）
market_review.py     — 市场环境门控 + 大盘复盘
```

📖 策略全文见 [docs/main_strategie.md](docs/main_strategie.md)

---

## ⏰ 每日执行时序（GitHub Actions）

```
北京时间
15:00  A股收盘
15:30  ① smart_screen        → 初筛候选股，写入妙想自选
16:00  ② daily_simple_analysis → 技术分析 + 买卖信号报告 + 通知
16:30  ③ market_review        → 大盘复盘 + 通知
```

---

## 🚀 快速开始（GitHub Actions）

### 1. Fork 本仓库

点击右上角 `Fork`

### 2. 配置 Secrets

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

**必填**

| Secret | 说明 |
|--------|------|
| `MX_APIKEY` | 妙想 API Key（用于自选股选股） |

**通知渠道（至少配一个）**

| Secret | 说明 |
|--------|------|
| `FEISHU_WEBHOOK_URL` | 飞书群机器人 Webhook |
| `DINGTALK_WEBHOOK_URL` | 钉钉群机器人 Webhook |
| `DISCORD_WEBHOOK_URL` | Discord Webhook |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECEIVERS` | 邮件通知 |

**可选**

| Secret / Variable | 说明 |
|--------|------|
| `SMART_SCREEN_KEYWORD` | 选股条件（留空则需手动触发时指定） |
| `TUSHARE_TOKEN` | Tushare 数据源 Token |
| `GEMINI_API_KEY` | 大盘复盘 LLM 增强（不配置则跳过） |
| `BOCHA_API_KEYS` / `TAVILY_API_KEYS` | 搜索引擎（大盘复盘用，可选） |

### 3. 启用 Actions

`Actions` 标签 → `I understand my workflows, go ahead and enable them`

### 4. 手动测试

`Actions` → 选择对应 workflow → `Run workflow`

---

## 💻 本地运行

```bash
git clone <your-fork-url>
cd daily_stock_analysis

pip install -r requirements.txt

# 复制并填写配置
cp .env.example .env
# 编辑 .env，填入 MX_APIKEY 和通知渠道

# 主分析（技术信号报告）
python main.py

# 仅选股（写入妙想自选）
python mx_smart_screen.py

# 大盘复盘
python market_review.py

# 调试模式
python main.py --debug --no-notify
```

---

## ⚙️ 关键环境变量

```bash
# 妙想 API（必填）
MX_APIKEY=your_mx_apikey

# 选股条件（可在 Action 手动触发时覆盖）
SMART_SCREEN_KEYWORD=均线多头排列，涨幅2%-7%，换手率超过5%

# 通知（至少配一个）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
# DINGTALK_WEBHOOK_URL=...
# DISCORD_WEBHOOK_URL=...
# EMAIL_SENDER=sender@example.com
# EMAIL_PASSWORD=...
# EMAIL_RECEIVERS=receiver@example.com

# 可选数据源
TUSHARE_TOKEN=your_tushare_token
```

---

## 📊 策略摘要

| 阶段 | 规则 |
|------|------|
| **市场过滤** | 满足 2/5 项环境条件才允许开仓（否则空仓） |
| **选股池** | 均线多头（MA5>MA10>MA20）+ 涨幅 2-7% + 换手率>5% |
| **买点** | 主升中第一次分歧回踩 MA5，缩量 + 不破 5 日线 |
| **第一卖点** | 放量跌破 MA5 / 高位长阴 / 回撤≥5% → 减仓 50% |
| **第二卖点** | 连续 2 日跌破 MA10 / 放量跌破 MA10 → 清仓 |
| **止损** | 买入逻辑被否定收盘执行；闪崩盘中直接走 |

📖 完整策略见 [docs/main_strategie.md](docs/main_strategie.md)

---

## 📄 License

本项目基于 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 改造，遵循原项目 [MIT License](LICENSE)。

---

> ⚠️ **免责声明**：本工具仅供个人学习研究使用，不构成投资建议。股市有风险，投资需谨慎。
