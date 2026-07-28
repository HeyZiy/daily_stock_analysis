# AGENTS.md — Trend Sniper

## Architecture

```
trend_analysis.py   → 趋势交易策略：信号检测 + 门控过滤 + 报告 + 通知
stock_selector.py   → 选股器：MX API 智能选股，写入妙想自选
etf_allocation.py   → ETF 长期配置：再平衡分析 + 盘中执行

src/analysis/market_gate.py  ← 共享门控模块（硬拦截 + 4项条件 + 5级状态）
src/analysis/report.py        ← Markdown 日报生成
src/analysis/strategy/         ← 信号检测(signal_detector.py)、剔除规则(removal_rules.py)
src/etf/                       ← ETF 门控、配置、再平衡引擎
src/notify/                    ← 多渠道通知（飞书/钉钉/Discord/邮件）
src/mx/                        ← 妙想模拟仓 API 客户端
data_provider/                 ← 多源行情数据（efinance > akshare > tushare > baostock > yfinance）
```

## Commands

```bash
python trend_analysis.py                    # 日度趋势分析
python trend_analysis.py --debug --no-notify
python trend_analysis.py --stocks 000001,600519

python stock_selector.py "均线多头，涨幅2%-7%"

python etf_allocation.py                    # ETF 盘后分析
python etf_allocation.py --execute          # ETF 盘中调仓
```

No test suite, no lint/typecheck commands.

## Config

- `.env` at project root, loaded via `python-dotenv` in `src/config.py:setup_env()`
- `setup_env()` must be called before importing config-dependent modules
- Required: `MX_APIKEY`
- `Config` is a singleton dataclass accessed via `Config.get_instance()`

## CI

- GitHub Actions, Python 3.11 on `ubuntu-latest`
- Runs weekdays at 13:30 Beijing time (UTC 05:30)
- Auto-tag on `main` push commits containing `#patch`, `#minor`, or `#major`
- Release auto-created on `v*.*.*` annotated tag push

## Design Decisions

- `docs/*.md` are maintenance documents, **not authoritative references**. They can fall out of sync. When making decisions about thresholds, trade logic, or signal priority, think from an investment/trading logic perspective — what makes sense for the strategy — not "what did the doc say."

## Key Conventions

- Chinese docstrings and comments throughout
- `data/` holds cached state (e.g. `market_gate_ice_days.json` for hard intercept consecutive-day tracking)
- Logging via `src/logging_config.py:setup_logging()` — console + file + debug file handlers
- All stock codes normalized via `data_provider.base:canonical_stock_code()`
