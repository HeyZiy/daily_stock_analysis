# 📈 个人股票智能分析助手

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 🤖 基于 AI 大模型的个人自选股每日自动分析工具
> 
> 每日定时分析自选股，推送「决策仪表盘」到飞书/钉钉/Discord/邮箱
>
> 本项目基于 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 简化定制，保留核心分析功能，移除 WebUI 和复杂配置，专注于个人每日自动分析场景。

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **AI 分析** | 一句话核心结论 + 买卖点位 + 操作检查清单 |
| **多维度** | 技术面 + 筹码分布 + 舆情情报 + 实时行情 |
| **多市场** | A股、港股、美股 |
| **自动推送** | 每日定时分析，多渠道推送 |
| **零成本** | GitHub Actions 免费运行，无需服务器 |

---

## 🚀 快速开始

### 方式：GitHub Actions（推荐）

> 5 分钟完成部署，零成本，无需服务器。

#### 1. Fork 本仓库

点击右上角 `Fork` 按钮

#### 2. 配置 Secrets

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

**AI 模型配置（至少配置一个）**

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/) 免费 Key | ✅ 推荐 |
| `DEEPSEEK_API_KEY` | [DeepSeek](https://platform.deepseek.com/) Key（作为 fallback） | 可选 |

**通知渠道（至少配置一个）**

| Secret 名称 | 说明 |
|------------|------|
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook URL |
| `DINGTALK_WEBHOOK_URL` | 钉钉 Webhook URL |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECEIVERS` | 邮件通知 |

**自选股配置**

| Secret 名称 | 说明 | 示例 |
|------------|------|------|
| `STOCK_LIST` | 自选股代码 | `600519,000858,hk00700,AAPL` |

#### 3. 启用 Actions

`Actions` 标签 → `I understand my workflows, go ahead and enable them`

#### 4. 手动测试

`Actions` → `每日股票分析` → `Run workflow` → `Run workflow`

---

## ⚙️ 配置说明

### 环境变量（.env）

```bash
# AI 模型
GEMINI_API_KEY=your_gemini_key
DEEPSEEK_API_KEY=your_deepseek_key  # 可选，作为 fallback

# 自选股
STOCK_LIST=600519,000858,hk00700,AAPL

# 通知渠道（配置你需要的）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

### 股票代码格式

| 市场 | 格式 | 示例 |
|------|------|------|
| A股 | 6位数字 | `600519`, `000858` |
| 港股 | hk+5位数字 | `hk00700`, `hk09988` |
| 美股 | 字母代码 | `AAPL`, `TSLA` |

---

## 📝 工作原理

```
GitHub Actions 定时触发（默认工作日 18:00）
        ↓
获取自选股列表
        ↓
对每只股票：
  ├─ 获取实时行情
  ├─ 获取历史数据
  ├─ 获取新闻舆情
  └─ LLM 分析生成报告
        ↓
推送到配置的渠道
```

### AI 模型优先级

1. **Gemini**（主模型）
2. **DeepSeek**（fallback，当 Gemini 额度耗尽时自动切换）

---

## 🔧 本地运行

```bash
# 克隆项目
git clone https://github.com/yourname/daily_stock_analysis.git
cd daily_stock_analysis

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API Key

# 运行分析
python main.py
```

---

## 📊 推送效果

每日推送包含：
- 📌 **核心结论**：买入/观望/卖出建议
- 📈 **技术信号**：均线排列、支撑压力位
- 🎯 **精确点位**：买入价、止损价、目标价
- 📰 **舆情情报**：相关新闻摘要
- ✅ **检查清单**：各项条件满足情况

---

## 💡 常见问题

**Q: Gemini 免费额度是多少？**
> 20 次/天。额度耗尽后自动切换到 DeepSeek（如果配置了）。

**Q: 可以分析多少只股票？**
> 取决于 API 额度。Gemini 免费版建议不超过 15 只。

**Q: 非交易日会执行吗？**
> 默认不会。如需测试，可手动触发 Actions。

---

## 📄 License & 致谢

本项目基于 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 进行简化定制，遵循原项目的 [MIT License](LICENSE)。

感谢原作者 ZhuLinsen 的开源贡献。

---

> ⚠️ **免责声明**：本工具仅供个人学习研究使用，不构成投资建议。股市有风险，投资需谨慎。
