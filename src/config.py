# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 配置管理模块
===================================

职责：
1. 使用单例模式管理全局配置
2. 从 .env 文件加载敏感配置
3. 提供类型安全的配置访问接口
"""

import logging
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv, dotenv_values

logger = logging.getLogger(__name__)


def _is_proxy_available(proxy_url: Optional[str]) -> bool:
    """
    检测代理是否可用
    
    Args:
        proxy_url: 代理URL，如 http://127.0.0.1:7897
        
    Returns:
        代理是否可用
    """
    if not proxy_url:
        return False
    
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port
        
        if not host or not port:
            return False
        
        # 尝试连接代理端口
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)  # 2秒超时
        result = sock.connect_ex((host, port))
        sock.close()
        
        return result == 0
    except Exception:
        return False


@dataclass
class ConfigIssue:
    """Structured configuration validation issue with a severity level.

    Attributes:
        severity: One of "error", "warning", or "info".
        message:  Human-readable description of the issue.
        field:    The environment variable / config field name most relevant to
                  this issue (empty string when not applicable).
    """

    severity: Literal["error", "warning", "info"]
    message: str
    field: str = ""

    def __str__(self) -> str:  # noqa: D105
        return self.message


_MANAGED_LITELLM_KEY_PROVIDERS = {"gemini", "vertex_ai", "anthropic", "openai", "deepseek"}
SUPPORTED_LLM_CHANNEL_PROTOCOLS = ("openai", "anthropic", "gemini", "vertex_ai", "deepseek", "ollama")
_FALSEY_ENV_VALUES = {"0", "false", "no", "off"}
NEWS_STRATEGY_WINDOWS: Dict[str, int] = {
    "ultra_short": 1,
    "short": 3,
    "medium": 7,
    "long": 30,
}


def parse_env_bool(value: Optional[str], default: bool = False) -> bool:
    """Parse common truthy/falsey environment-style values."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized not in _FALSEY_ENV_VALUES


def normalize_news_strategy_profile(value: Optional[str]) -> str:
    """Normalize news strategy profile to known values."""
    candidate = (value or "short").strip().lower()
    return candidate if candidate in NEWS_STRATEGY_WINDOWS else "short"


def resolve_news_window_days(news_max_age_days: int, news_strategy_profile: Optional[str]) -> int:
    """Resolve effective news window days from profile and global max-age."""
    profile = normalize_news_strategy_profile(news_strategy_profile)
    profile_days = NEWS_STRATEGY_WINDOWS.get(profile, NEWS_STRATEGY_WINDOWS["short"])
    return max(1, min(max(1, int(news_max_age_days)), profile_days))


def canonicalize_llm_channel_protocol(value: Optional[str]) -> str:
    """Normalize a protocol label into a LiteLLM provider identifier."""
    candidate = (value or "").strip().lower().replace("-", "_")
    aliases = {
        "openai_compatible": "openai",
        "openai_compat": "openai",
        "claude": "anthropic",
        "google": "gemini",
        "vertex": "vertex_ai",
        "vertexai": "vertex_ai",
    }
    return aliases.get(candidate, candidate)


def resolve_llm_channel_protocol(
    protocol: Optional[str],
    *,
    base_url: Optional[str] = None,
    models: Optional[List[str]] = None,
    channel_name: Optional[str] = None,
) -> str:
    """Resolve the effective protocol for a channel."""
    explicit = canonicalize_llm_channel_protocol(protocol)
    if explicit in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
        return explicit

    for model in models or []:
        if "/" not in model:
            continue
        prefix = canonicalize_llm_channel_protocol(model.split("/", 1)[0])
        if prefix in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
            return prefix

    # Infer from channel name (e.g. "deepseek" -> deepseek, "gemini" -> gemini)
    if channel_name:
        name_protocol = canonicalize_llm_channel_protocol(channel_name)
        if name_protocol in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
            return name_protocol

    if base_url:
        parsed = urlparse(base_url)
        if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
            # Default to openai for local servers (vLLM, LM Studio, LocalAI, etc.).
            # Ollama users should set PROTOCOL=ollama explicitly or name the channel "ollama".
            return "openai"
        return "openai"

    return ""


def channel_allows_empty_api_key(protocol: Optional[str], base_url: Optional[str]) -> bool:
    """Return True when a channel can run without an API key."""
    resolved_protocol = resolve_llm_channel_protocol(protocol, base_url=base_url)
    if resolved_protocol == "ollama":
        return True
    parsed = urlparse(base_url or "")
    return parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def normalize_llm_channel_model(model: str, protocol: Optional[str], base_url: Optional[str] = None) -> str:
    """Attach a provider prefix when the model omits it."""
    normalized_model = model.strip()
    if not normalized_model:
        return normalized_model

    resolved_protocol = resolve_llm_channel_protocol(protocol, base_url=base_url, models=[normalized_model])

    if "/" in normalized_model:
        # The model already has a slash, e.g. 'deepseek-ai/DeepSeek-V3'.
        # Check if the prefix is a known LiteLLM provider; if so, keep it.
        # Otherwise (e.g. HuggingFace-style IDs on SiliconFlow), prepend
        # the resolved protocol so LiteLLM routes via the correct handler.
        raw_prefix, remainder = normalized_model.split("/", 1)
        prefix = raw_prefix.lower()
        canonical_prefix = canonicalize_llm_channel_protocol(prefix)
        known_providers = _MANAGED_LITELLM_KEY_PROVIDERS | set(SUPPORTED_LLM_CHANNEL_PROTOCOLS) | {
            "cohere", "huggingface", "bedrock", "sagemaker", "azure",
            "replicate", "together_ai", "palm", "text-completion-openai",
            "command-r", "groq", "cerebras", "fireworks_ai", "friendliai",
        }
        if prefix in known_providers:
            return normalized_model
        if canonical_prefix in known_providers:
            return f"{canonical_prefix}/{remainder}"
        # Not a real provider prefix — add one so LiteLLM routes correctly.
        if resolved_protocol:
            return f"{resolved_protocol}/{normalized_model}"
        return normalized_model

    if not resolved_protocol:
        return normalized_model
    return f"{resolved_protocol}/{normalized_model}"


def get_configured_llm_models(model_list: List[Dict[str, Any]]) -> List[str]:
    """Return non-legacy model names declared in Router model_list order.

    Uses the top-level ``model_name`` (the routing alias that users set in
    LITELLM_MODEL) rather than ``litellm_params.model`` (the wire-level
    model identifier).  For channel-built entries both are identical, but
    YAML configs may define a friendly alias that differs from the
    underlying provider/model path.
    """
    models: List[str] = []
    seen: set = set()
    for entry in model_list or []:
        # Prefer top-level model_name (router routing key); fall back to
        # litellm_params.model for entries that omit it.
        name = str(entry.get("model_name") or "").strip()
        if not name:
            params = entry.get("litellm_params", {}) or {}
            name = str(params.get("model") or "").strip()
        if not name or name.startswith("__legacy_") or name in seen:
            continue
        seen.add(name)
        models.append(name)
    return models


def resolve_unified_llm_temperature(model: str) -> float:
    """Resolve the unified LLM temperature with backward-compatible fallbacks."""
    llm_temperature_raw = os.getenv("LLM_TEMPERATURE")
    if llm_temperature_raw and llm_temperature_raw.strip():
        try:
            return float(llm_temperature_raw)
        except (ValueError, TypeError):
            pass

    provider_temperature_env = {
        "gemini": "GEMINI_TEMPERATURE",
        "vertex_ai": "GEMINI_TEMPERATURE",
        "anthropic": "ANTHROPIC_TEMPERATURE",
        "openai": "OPENAI_TEMPERATURE",
        "deepseek": "OPENAI_TEMPERATURE",
    }
    preferred_env = provider_temperature_env.get(_get_litellm_provider(model))
    if preferred_env:
        preferred_value = os.getenv(preferred_env)
        if preferred_value and preferred_value.strip():
            try:
                return float(preferred_value)
            except (ValueError, TypeError):
                pass

    for env_name in ("GEMINI_TEMPERATURE", "ANTHROPIC_TEMPERATURE", "OPENAI_TEMPERATURE"):
        env_value = os.getenv(env_name)
        if env_value and env_value.strip():
            try:
                return float(env_value)
            except (ValueError, TypeError):
                continue

    return 0.7


def _get_litellm_provider(model: str) -> str:
    """Extract the LiteLLM provider prefix from a model string."""
    if not model:
        return ""
    if "/" in model:
        return model.split("/", 1)[0]
    return "openai"


def _uses_direct_env_provider(model: str) -> bool:
    """Whether runtime handles the model via direct litellm env/provider resolution."""
    provider = _get_litellm_provider(model)
    return bool(provider) and provider not in _MANAGED_LITELLM_KEY_PROVIDERS


def setup_env(override: bool = False):
    """
    Initialize environment variables from .env file.

    Args:
        override: If True, overwrite existing environment variables with values
                  from .env file. Set to True when reloading config after updates.
                  Default is False to preserve behavior on initial load where
                  system environment variables take precedence.
    """
    # src/config.py -> src/ -> root
    env_file = os.getenv("ENV_FILE")
    if env_file:
        env_path = Path(env_file)
    else:
        env_path = Path(__file__).parent.parent / '.env'
    load_dotenv(dotenv_path=env_path, override=override)


@dataclass
class Config:
    """
    系统配置类 - 单例模式
    
    设计说明：
    - 使用 dataclass 简化配置属性定义
    - 所有配置项从环境变量读取，支持默认值
    - 类方法 get_instance() 实现单例访问
    """
    
    # === 自选股配置 ===
    stock_list: List[str] = field(default_factory=list)

    # === 飞书云文档配置 ===
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_folder_token: Optional[str] = None  # 目标文件夹 Token

    # === 数据源 API Token ===
    tushare_token: Optional[str] = None
    mx_apikey: Optional[str] = None
    smart_screen_keyword: Optional[str] = None
    
    # === AI 分析配置 ===
    # LiteLLM unified model config (provider/model format, e.g. gemini/gemini-2.5-flash)
    litellm_model: str = ""  # Primary model; must include provider prefix when set explicitly
    litellm_fallback_models: List[str] = field(default_factory=list)  # Cross-model fallback list

    # Unified temperature for all LLM calls (LLM_TEMPERATURE); legacy per-provider temps are fallback only
    llm_temperature: float = 0.7

    # --- Multi-channel LLM config (new) ---
    # LITELLM_CONFIG: path to a standard litellm_config.yaml file (most powerful)
    litellm_config_path: Optional[str] = None
    # Internal metadata: which config layer actually produced llm_model_list
    llm_models_source: str = "legacy_env"
    # LLM_CHANNELS: list of channel dicts, each with name/base_url/api_keys/models
    llm_channels: List[Dict[str, Any]] = field(default_factory=list)
    # Pre-built LiteLLM Router model_list (populated from channels, YAML, or legacy keys)
    llm_model_list: List[Dict[str, Any]] = field(default_factory=list)

    # --- Simple LLM config (for llm_client.py) ---
    gemini_api_keys: List[str] = field(default_factory=list)  # Gemini API Keys
    deepseek_api_keys: List[str] = field(default_factory=list)  # DeepSeek API Keys
    deepseek_base_url: str = "https://api.deepseek.com/v1"  # DeepSeek API Base URL



    # === Vision 配置 ===
    # VISION_MODEL: litellm model string used for image understanding calls.
    # Fallback chain: VISION_MODEL → OPENAI_VISION_MODEL → gemini/gemini-2.0-flash
    vision_model: str = ""
    # VISION_PROVIDER_PRIORITY: comma-separated provider order for Vision fallback.
    vision_provider_priority: str = "gemini"

    # === 搜索引擎配置（支持多 Key 负载均衡）===
    bocha_api_keys: List[str] = field(default_factory=list)  # Bocha API Keys
    minimax_api_keys: List[str] = field(default_factory=list)  # MiniMax API Keys
    tavily_api_keys: List[str] = field(default_factory=list)  # Tavily API Keys
    brave_api_keys: List[str] = field(default_factory=list)  # Brave Search API Keys
    searxng_base_urls: List[str] = field(default_factory=list)  # SearXNG instance URLs (self-hosted, no quota)

    # === Social Sentiment (US stocks only, api.adanos.org) ===
    social_sentiment_api_key: Optional[str] = None
    social_sentiment_api_url: str = "https://api.adanos.org"

    # === 新闻与分析筛选配置 ===
    news_max_age_days: int = 3   # 新闻最大时效（天）
    news_strategy_profile: str = "short"  # 新闻窗口策略档位：ultra_short/short/medium/long
    bias_threshold: float = 5.0  # 乖离率阈值（%），超过此值提示不追高

    # === Agent 模式配置 ===
    agent_mode: bool = False
    _agent_mode_explicit: bool = False  # True when AGENT_MODE was explicitly set in env
    agent_max_steps: int = 10
    agent_skills: List[str] = field(default_factory=list)
    agent_strategy_dir: Optional[str] = None
    agent_nl_routing: bool = False  # Enable natural language routing in bot dispatcher
    agent_arch: str = "single"     # Agent architecture: 'single' (legacy) or 'multi' (orchestrator)
    agent_orchestrator_mode: str = "standard"  # Orchestrator mode: quick/standard/full/strategy
    agent_orchestrator_timeout_s: int = 600  # Cooperative timeout budget for the whole multi-agent pipeline
    agent_risk_override: bool = True  # Allow risk agent to veto buy signals
    agent_deep_research_budget: int = 30000  # Max token budget for deep research
    agent_deep_research_timeout: int = 180  # Max seconds for /research command before returning timeout
    agent_memory_enabled: bool = False  # Enable memory & calibration system
    agent_strategy_autoweight: bool = True  # Auto-weight strategies by backtest performance
    agent_strategy_routing: str = "auto"  # Strategy routing: 'auto' (regime-based) or 'manual'
    agent_event_monitor_enabled: bool = False  # Enable periodic event-driven alert checks in schedule mode
    agent_event_monitor_interval_minutes: int = 5  # Polling interval for event monitor background checks
    agent_event_alert_rules_json: str = ""  # JSON array of serialized EventMonitor rules

    # === 通知配置（可同时配置多个，全部推送）===
    
    # 企业微信 Webhook
    wechat_webhook_url: Optional[str] = None
    
    # 飞书 Webhook
    feishu_webhook_url: Optional[str] = None
    
    # Telegram 配置（需要同时配置 Bot Token 和 Chat ID）
    telegram_bot_token: Optional[str] = None  # Bot Token（@BotFather 获取）
    telegram_chat_id: Optional[str] = None  # Chat ID
    telegram_message_thread_id: Optional[str] = None  # Topic ID (Message Thread ID) for groups
    
    # 邮件配置（只需邮箱和授权码，SMTP 自动识别）
    email_sender: Optional[str] = None  # 发件人邮箱
    email_sender_name: str = "daily_stock_analysis股票分析助手"  # 发件人显示名称
    email_password: Optional[str] = None  # 邮箱密码/授权码
    email_receivers: List[str] = field(default_factory=list)  # 收件人列表（留空则发给自己）

    # Stock-to-email group routing (Issue #268): STOCK_GROUP_N + EMAIL_GROUP_N
    # When configured, each group's report is sent to that group's emails only.
    stock_email_groups: List[Tuple[List[str], List[str]]] = field(default_factory=list)

    # Pushover 配置（手机/桌面推送通知）
    pushover_user_key: Optional[str] = None  # 用户 Key（https://pushover.net 获取）
    pushover_api_token: Optional[str] = None  # 应用 API Token
    
    # 自定义 Webhook（支持多个，逗号分隔）
    # 适用于：钉钉、Discord、Slack、自建服务等任意支持 POST JSON 的 Webhook
    custom_webhook_urls: List[str] = field(default_factory=list)
    custom_webhook_bearer_token: Optional[str] = None  # Bearer Token（用于需要认证的 Webhook）
    webhook_verify_ssl: bool = True  # Webhook HTTPS 证书校验，false 可支持自签名（有 MITM 风险）

    # Discord 通知配置
    discord_bot_token: Optional[str] = None  # Discord Bot Token
    discord_main_channel_id: Optional[str] = None  # Discord 主频道 ID
    discord_webhook_url: Optional[str] = None  # Discord Webhook URL

    # AstrBot 通知配置
    astrbot_token: Optional[str] = None
    astrbot_url: Optional[str] = None

    # 单股推送模式：每分析完一只股票立即推送，而不是汇总后推送
    single_stock_notify: bool = False

    # 报告类型：simple(精简) 或 full(完整)
    report_type: str = "simple"

    # 仅分析结果摘要：true 时只推送汇总，不含个股详情（Issue #262）
    report_summary_only: bool = False

    # Report Engine P0: Jinja2 renderer and integrity checks
    report_templates_dir: str = "templates"  # Template directory (relative to project root)
    report_renderer_enabled: bool = False  # Enable Jinja2 rendering (default off for zero regression)
    report_integrity_enabled: bool = True  # Content integrity validation after LLM output
    report_integrity_retry: int = 1  # Retry count when mandatory fields missing (0 = placeholder only)
    report_history_compare_n: int = 0  # History comparison count (0 = disabled)

    # PushPlus 推送配置
    pushplus_token: Optional[str] = None  # PushPlus Token
    pushplus_topic: Optional[str] = None  # PushPlus 群组编码（一对多推送）

    # Server酱3 推送配置
    serverchan3_sendkey: Optional[str] = None  # Server酱3 SendKey

    # 分析间隔时间（秒）- 用于避免API限流
    analysis_delay: float = 0.0  # 个股分析与大盘分析之间的延迟
    gemini_request_delay: float = 0.0  # Gemini API 请求前延迟（秒），用于避免触发限流

    # Merge stock + market report into one notification (Issue #190)
    merge_email_notification: bool = False

    # 消息长度限制（字节）- 超长自动分批发送
    feishu_max_bytes: int = 20000  # 飞书限制约 20KB，默认 20000 字节
    wechat_max_bytes: int = 4000   # 企业微信限制 4096 字节，默认 4000 字节
    discord_max_words: int = 2000  # Discord 限制 2000 字，默认 2000 字
    wechat_msg_type: str = "markdown"  # 企业微信消息类型，默认 markdown 类型

    # Markdown 转图片（Issue #289）：对不支持 Markdown 的渠道以图片发送
    markdown_to_image_channels: List[str] = field(default_factory=list)  # 逗号分隔：telegram,wechat,custom,email
    markdown_to_image_max_chars: int = 15000  # 超过此长度不转换，避免超大图片
    md2img_engine: str = "wkhtmltoimage"  # wkhtmltoimage | markdown-to-file (Issue #455, better emoji support)

    # 实时行情预取（Issue #455）：设为 false 可禁用，避免 efinance/akshare_em 全市场拉取
    prefetch_realtime_quotes: bool = True

    # === 日志配置 ===
    log_dir: str = "./logs"  # 日志文件目录
    log_level: str = "INFO"  # 日志级别
    
    # === 系统配置 ===
    max_workers: int = 3  # 低并发防封禁
    debug: bool = False
    http_proxy: Optional[str] = None  # HTTP 代理 (例如: http://127.0.0.1:10809)
    https_proxy: Optional[str] = None # HTTPS 代理
    
    # === 定时任务配置 ===
    schedule_enabled: bool = False            # 是否启用定时任务
    schedule_time: str = "18:00"              # 每日推送时间（HH:MM 格式）
    schedule_run_immediately: bool = True     # 启动时是否立即执行一次
    run_immediately: bool = True              # 启动时是否立即执行一次（非定时模式）
    # === 实时行情增强数据配置 ===
    # 实时行情开关（关闭后使用历史收盘价进行分析）
    enable_realtime_quote: bool = True
    # 盘中实时技术面：启用时用实时价计算 MA/多头排列（Issue #234）；关闭则用昨日收盘
    enable_realtime_technical_indicators: bool = True
    # 筹码分布开关（该接口不稳定，云端部署建议关闭）
    enable_chip_distribution: bool = True

    # 实时行情数据源优先级（逗号分隔）
    # 推荐顺序：tencent > akshare_sina > efinance > akshare_em > tushare
    # - tencent: 腾讯财经，有量比/换手率/市盈率等，单股查询稳定（推荐）
    # - akshare_sina: 新浪财经，基本行情稳定，但无量比
    # - efinance/akshare_em: 东财全量接口，数据最全但容易被封
    # - tushare: Tushare Pro，需要2000积分，数据全面（付费用户可优先使用）
    realtime_source_priority: str = "tencent,akshare_sina,efinance,akshare_em"
    # 实时行情缓存时间（秒）
    realtime_cache_ttl: int = 600
    # 熔断器冷却时间（秒）
    circuit_breaker_cooldown: int = 300

    # === 基本面聚合开关与降级保护 ===
    # 全局总开关；关闭时返回 not_supported 并保持主流程无变化
    enable_fundamental_pipeline: bool = True
    # 基本面阶段总预算（秒）
    fundamental_stage_timeout_seconds: float = 1.5
    # 单能力源调用超时（秒）
    fundamental_fetch_timeout_seconds: float = 0.8
    # 单能力失败重试次数（已包含首次）
    fundamental_retry_max: int = 1
    # 基本面上下文短 TTL（秒）
    fundamental_cache_ttl_seconds: int = 120
    # 基本面缓存最大条目数（避免长时间运行内存增长）
    fundamental_cache_max_entries: int = 256

    # === Portfolio PR2: import/risk/fx settings ===
    portfolio_risk_concentration_alert_pct: float = 35.0
    portfolio_risk_drawdown_alert_pct: float = 15.0
    portfolio_risk_stop_loss_alert_pct: float = 10.0
    portfolio_risk_stop_loss_near_ratio: float = 0.8
    portfolio_risk_lookback_days: int = 180
    portfolio_fx_update_enabled: bool = True

    # Discord 机器人状态
    discord_bot_status: str = "A股智能分析 | /help"

    # === 流控配置（防封禁关键参数）===
    # Akshare 请求间隔范围（秒）
    akshare_sleep_min: float = 2.0
    akshare_sleep_max: float = 5.0
    
    # Tushare 每分钟最大请求数（免费配额）
    tushare_rate_limit_per_minute: int = 80
    
    # 重试配置
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    
    # === WebUI 配置 ===
    webui_enabled: bool = False
    webui_host: str = "127.0.0.1"
    webui_port: int = 8000
    
    # === 机器人配置 ===
    bot_enabled: bool = True              # 是否启用机器人功能
    bot_command_prefix: str = "/"         # 命令前缀
    bot_rate_limit_requests: int = 10     # 频率限制：窗口内最大请求数
    bot_rate_limit_window: int = 60       # 频率限制：窗口时间（秒）
    bot_admin_users: List[str] = field(default_factory=list)  # 管理员用户 ID 列表
    
    # 飞书机器人（事件订阅）- 已有 feishu_app_id, feishu_app_secret
    feishu_verification_token: Optional[str] = None  # 事件订阅验证 Token
    feishu_encrypt_key: Optional[str] = None         # 消息加密密钥（可选）
    feishu_stream_enabled: bool = False              # 是否启用 Stream 长连接模式（无需公网IP）
    
    # 钉钉机器人
    dingtalk_app_key: Optional[str] = None      # 应用 AppKey
    dingtalk_app_secret: Optional[str] = None   # 应用 AppSecret
    dingtalk_stream_enabled: bool = False       # 是否启用 Stream 模式（无需公网IP）
    
    # 企业微信机器人（回调模式）
    wecom_corpid: Optional[str] = None              # 企业 ID
    wecom_token: Optional[str] = None               # 回调 Token
    wecom_encoding_aes_key: Optional[str] = None    # 消息加解密密钥
    wecom_agent_id: Optional[str] = None            # 应用 AgentId
    
    # Telegram 机器人 - 已有 telegram_bot_token, telegram_chat_id
    telegram_webhook_secret: Optional[str] = None   # Webhook 密钥

    # === 配置校验模式 ===
    # CONFIG_VALIDATE_MODE=warn (default): log all issues but always continue startup
    # CONFIG_VALIDATE_MODE=strict: exit(1) when any "error" severity issue is found
    config_validate_mode: str = "warn"

    # --- Post-init validation ---------------------------------------------------
    _VALID_AGENT_ARCH = {"single", "multi"}
    _VALID_ORCHESTRATOR_MODES = {"quick", "standard", "full", "strategy"}
    _VALID_STRATEGY_ROUTING = {"auto", "manual"}

    def __post_init__(self) -> None:
        _log = logging.getLogger(__name__)
        if self.agent_arch not in self._VALID_AGENT_ARCH:
            _log.warning(
                "Invalid AGENT_ARCH=%r, falling back to 'single'. Valid: %s",
                self.agent_arch, self._VALID_AGENT_ARCH,
            )
            object.__setattr__(self, "agent_arch", "single")
        if self.agent_orchestrator_mode not in self._VALID_ORCHESTRATOR_MODES:
            _log.warning(
                "Invalid AGENT_ORCHESTRATOR_MODE=%r, falling back to 'standard'. Valid: %s",
                self.agent_orchestrator_mode, self._VALID_ORCHESTRATOR_MODES,
            )
            object.__setattr__(self, "agent_orchestrator_mode", "standard")
        if self.agent_strategy_routing not in self._VALID_STRATEGY_ROUTING:
            _log.warning(
                "Invalid AGENT_STRATEGY_ROUTING=%r, falling back to 'auto'. Valid: %s",
                self.agent_strategy_routing, self._VALID_STRATEGY_ROUTING,
            )
            object.__setattr__(self, "agent_strategy_routing", "auto")

    # 单例实例存储
    _instance: Optional['Config'] = None
    
    @classmethod
    def get_instance(cls) -> 'Config':
        """
        获取配置单例实例
        
        单例模式确保：
        1. 全局只有一个配置实例
        2. 配置只从环境变量加载一次
        3. 所有模块共享相同配置
        """
        if cls._instance is None:
            cls._instance = cls._load_from_env()
        return cls._instance
    
    @classmethod
    def _load_from_env(cls) -> 'Config':
        """
        从 .env 文件加载配置
        
        加载优先级：
        1. 系统环境变量
        2. .env 文件
        3. 代码中的默认值
        """
        # 确保环境变量已加载
        setup_env()

        # === 智能代理配置 (关键修复) ===
        # 检测代理可用性，如果代理不可用则清除代理设置，避免后续请求失败
        http_proxy = os.getenv('HTTP_PROXY') or os.getenv('http_proxy')
        https_proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
        
        # 检测 HTTP 代理可用性
        if http_proxy and not _is_proxy_available(http_proxy):
            logger.warning(f"HTTP 代理不可用 ({http_proxy})，已自动禁用代理")
            http_proxy = None
            for key in ['HTTP_PROXY', 'http_proxy']:
                if key in os.environ:
                    del os.environ[key]
        
        # 检测 HTTPS 代理可用性
        if https_proxy and not _is_proxy_available(https_proxy):
            logger.warning(f"HTTPS 代理不可用 ({https_proxy})，已自动禁用代理")
            https_proxy = None
            for key in ['HTTPS_PROXY', 'https_proxy']:
                if key in os.environ:
                    del os.environ[key]
        
        # 如果配置了代理，自动设置 NO_PROXY 以排除国内数据源，避免行情获取失败
        if http_proxy:
            # 国内金融数据源域名列表
            domestic_domains = [
                'eastmoney.com',   # 东方财富 (Efinance/Akshare)
                'sina.com.cn',     # 新浪财经 (Akshare)
                '163.com',         # 网易财经 (Akshare)
                'tushare.pro',     # Tushare
                'baostock.com',    # Baostock
                'sse.com.cn',      # 上交所
                'szse.cn',         # 深交所
                'csindex.com.cn',  # 中证指数
                'cninfo.com.cn',   # 巨潮资讯
                'localhost',
                '127.0.0.1'
            ]

            # 获取现有的 no_proxy
            current_no_proxy = os.getenv('NO_PROXY') or os.getenv('no_proxy') or ''
            existing_domains = current_no_proxy.split(',') if current_no_proxy else []

            # 合并去重
            final_domains = list(set(existing_domains + domestic_domains))
            final_no_proxy = ','.join(filter(None, final_domains))

            # 设置环境变量 (requests/urllib3/aiohttp 都会遵守此设置)
            os.environ['NO_PROXY'] = final_no_proxy
            os.environ['no_proxy'] = final_no_proxy

            # 确保 HTTP_PROXY 也被正确设置（以防仅在 .env 中定义但未导出）
            os.environ['HTTP_PROXY'] = http_proxy
            os.environ['http_proxy'] = http_proxy

            # HTTPS_PROXY 同理
            if https_proxy:
                os.environ['HTTPS_PROXY'] = https_proxy
                os.environ['https_proxy'] = https_proxy

        
        # 解析自选股列表（逗号分隔，统一为大写 Issue #355）
        stock_list_str = os.getenv('STOCK_LIST', '')
        stock_list = [
            (c or "").strip().upper()
            for c in stock_list_str.split(',')
            if (c or "").strip()
        ]
        
        # 如果没有配置，使用默认的示例股票
        if not stock_list:
            stock_list = ['600519', '000001', '300750']
        
        # === LLM Channels config (唯一支持的配置方式) ===
        litellm_config_path = os.getenv('LITELLM_CONFIG', '').strip() or None
        llm_models_source = ""
        llm_channels: List[Dict[str, Any]] = []
        llm_model_list: List[Dict[str, Any]] = []

        # Priority 1: LITELLM_CONFIG (standard LiteLLM YAML config file)
        if litellm_config_path:
            llm_model_list = cls._parse_litellm_yaml(litellm_config_path)
            if llm_model_list:
                llm_models_source = "litellm_config"

        # Priority 2: LLM_CHANNELS (env var based channel config)
        if not llm_model_list:
            _channels_str = os.getenv('LLM_CHANNELS', '').strip()
            if _channels_str:
                llm_channels = cls._parse_llm_channels(_channels_str)
                llm_model_list = cls._channels_to_model_list(llm_channels)
                if llm_model_list:
                    llm_models_source = "llm_channels"

        # 注意：原生配置（GEMINI_API_KEY, ANTHROPIC_API_KEY 等）已不再支持
        # 请使用 LLM_CHANNELS 或 LITELLM_CONFIG 配置模型

        # 读取 LITELLM_MODEL 和 LITELLM_FALLBACK_MODELS
        litellm_model = os.getenv('LITELLM_MODEL', '').strip()
        _fallback_str = os.getenv('LITELLM_FALLBACK_MODELS', '')
        litellm_fallback_models = [m.strip() for m in _fallback_str.split(',') if m.strip()]

        # 如果没有设置 LITELLM_MODEL，尝试从渠道配置中推断
        if not litellm_model and llm_channels:
            for _ch in llm_channels:
                if _ch.get('models'):
                    litellm_model = _ch['models'][0]
                    break

        # 如果没有设置 LITELLM_FALLBACK_MODELS，尝试从渠道配置中推断
        if not litellm_fallback_models and llm_channels and litellm_model:
            _all_ch_models: List[str] = []
            for _ch in llm_channels:
                _all_ch_models.extend(_ch.get('models', []))
            _seen = {litellm_model}
            litellm_fallback_models = [
                m for m in _all_ch_models
                if m not in _seen and not _seen.add(m)  # type: ignore[func-returns-value]
            ]

        # 解析搜索引擎 API Keys
        # 记录调试信息 (不输出 key 本身)
        raw_mx = os.getenv('MX_APIKEY')
        raw_mx_alt = os.getenv('MX_API_KEY')
        
        mx_apikey = (raw_mx or raw_mx_alt or '').strip() or None
        smart_screen_keyword = (
            os.getenv('SMART_SCREEN_KEYWORD') or
            ''
        ).strip() or None

        bocha_keys_str = os.getenv('BOCHA_API_KEYS', '')
        bocha_api_keys = [k.strip() for k in bocha_keys_str.split(',') if k.strip()]

        minimax_keys_str = os.getenv('MINIMAX_API_KEYS', '')
        minimax_api_keys = [k.strip() for k in minimax_keys_str.split(',') if k.strip()]
        
        tavily_keys_str = os.getenv('TAVILY_API_KEYS', '')
        tavily_api_keys = [k.strip() for k in tavily_keys_str.split(',') if k.strip()]
        
        brave_keys_str = os.getenv('BRAVE_API_KEYS', '')
        brave_api_keys = [k.strip() for k in brave_keys_str.split(',') if k.strip()]

        _raw_urls = [u.strip() for u in os.getenv('SEARXNG_BASE_URLS', '').split(',') if u.strip()]
        searxng_base_urls = []
        invalid_searxng_urls = []
        for u in _raw_urls:
            p = urlparse(u)
            if p.scheme in ('http', 'https') and p.netloc:
                searxng_base_urls.append(u)
            else:
                invalid_searxng_urls.append(u)
        if invalid_searxng_urls:
            import logging
            logging.getLogger(__name__).warning(
                "SEARXNG_BASE_URLS 中存在无效 URL，已忽略: %s",
                ", ".join(invalid_searxng_urls[:3]),
            )

        # 企微消息类型与最大字节数逻辑
        wechat_msg_type = os.getenv('WECHAT_MSG_TYPE', 'markdown')
        wechat_msg_type_lower = wechat_msg_type.lower()
        wechat_max_bytes_env = os.getenv('WECHAT_MAX_BYTES')
        if wechat_max_bytes_env not in (None, ''):
            wechat_max_bytes = int(wechat_max_bytes_env)
        else:
            # 未显式配置时，根据消息类型选择默认字节数
            wechat_max_bytes = 2048 if wechat_msg_type_lower == 'text' else 4000
        
        # 解析简单 LLM 配置（用于 llm_client.py）
        gemini_api_keys = [k.strip() for k in os.getenv('GEMINI_API_KEY', '').split(',') if k.strip()]
        deepseek_api_keys = [k.strip() for k in os.getenv('DEEPSEEK_API_KEY', '').split(',') if k.strip()]
        deepseek_base_url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1').rstrip('/')

        return cls(
            stock_list=stock_list,
            feishu_app_id=os.getenv('FEISHU_APP_ID'),
            feishu_app_secret=os.getenv('FEISHU_APP_SECRET'),
            feishu_folder_token=os.getenv('FEISHU_FOLDER_TOKEN'),
            tushare_token=os.getenv('TUSHARE_TOKEN'),
            litellm_model=litellm_model,
            litellm_fallback_models=litellm_fallback_models,
            llm_temperature=resolve_unified_llm_temperature(litellm_model),
            litellm_config_path=litellm_config_path,
            llm_models_source=llm_models_source,
            llm_channels=llm_channels,
            llm_model_list=llm_model_list,

            # Simple LLM config (for llm_client.py)
            gemini_api_keys=gemini_api_keys,
            deepseek_api_keys=deepseek_api_keys,
            deepseek_base_url=deepseek_base_url,

            # Vision model: VISION_MODEL > OPENAI_VISION_MODEL (alias) > default
            vision_model=(
                os.getenv('VISION_MODEL')
                or os.getenv('OPENAI_VISION_MODEL')
                or ""
            ),
            vision_provider_priority=os.getenv('VISION_PROVIDER_PRIORITY', 'gemini'),
            bocha_api_keys=bocha_api_keys,
            minimax_api_keys=minimax_api_keys,
            tavily_api_keys=tavily_api_keys,
            brave_api_keys=brave_api_keys,
            searxng_base_urls=searxng_base_urls,
            social_sentiment_api_key=os.getenv('SOCIAL_SENTIMENT_API_KEY') or None,
            social_sentiment_api_url=os.getenv('SOCIAL_SENTIMENT_API_URL', 'https://api.adanos.org').rstrip('/'),
            news_max_age_days=max(1, int(os.getenv('NEWS_MAX_AGE_DAYS', '3'))),
            news_strategy_profile=cls._parse_news_strategy_profile(
                os.getenv('NEWS_STRATEGY_PROFILE', 'short')
            ),
            bias_threshold=max(1.0, float(os.getenv('BIAS_THRESHOLD', '5.0'))),
            agent_mode=os.getenv('AGENT_MODE', 'false').lower() == 'true',
            _agent_mode_explicit=os.getenv('AGENT_MODE') is not None,
            agent_max_steps=int(os.getenv('AGENT_MAX_STEPS', '10')),
            agent_skills=[s.strip() for s in os.getenv('AGENT_SKILLS', '').split(',') if s.strip()],
            agent_strategy_dir=os.getenv('AGENT_STRATEGY_DIR'),
            agent_nl_routing=os.getenv('AGENT_NL_ROUTING', 'false').lower() == 'true',
            agent_arch=os.getenv('AGENT_ARCH', 'single').lower(),
            agent_orchestrator_mode=os.getenv('AGENT_ORCHESTRATOR_MODE', 'standard').lower(),
            agent_orchestrator_timeout_s=max(0, int(os.getenv('AGENT_ORCHESTRATOR_TIMEOUT_S', '600'))),
            agent_risk_override=os.getenv('AGENT_RISK_OVERRIDE', 'true').lower() == 'true',
            agent_deep_research_budget=int(os.getenv('AGENT_DEEP_RESEARCH_BUDGET', '30000')),
            agent_deep_research_timeout=max(30, int(os.getenv('AGENT_DEEP_RESEARCH_TIMEOUT', '180'))),
            agent_memory_enabled=os.getenv('AGENT_MEMORY_ENABLED', 'false').lower() == 'true',
            agent_strategy_autoweight=os.getenv('AGENT_STRATEGY_AUTOWEIGHT', 'true').lower() == 'true',
            agent_strategy_routing=os.getenv('AGENT_STRATEGY_ROUTING', 'auto').lower(),
            agent_event_monitor_enabled=os.getenv('AGENT_EVENT_MONITOR_ENABLED', 'false').lower() == 'true',
            agent_event_monitor_interval_minutes=max(1, int(os.getenv('AGENT_EVENT_MONITOR_INTERVAL_MINUTES', '5'))),
            agent_event_alert_rules_json=os.getenv('AGENT_EVENT_ALERT_RULES_JSON', ''),
            wechat_webhook_url=os.getenv('WECHAT_WEBHOOK_URL'),
            feishu_webhook_url=os.getenv('FEISHU_WEBHOOK_URL'),
            telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN'),
            telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID'),
            telegram_message_thread_id=os.getenv('TELEGRAM_MESSAGE_THREAD_ID'),
            email_sender=os.getenv('EMAIL_SENDER'),
            email_sender_name=os.getenv('EMAIL_SENDER_NAME', 'daily_stock_analysis股票分析助手'),
            email_password=os.getenv('EMAIL_PASSWORD'),
            email_receivers=[r.strip() for r in os.getenv('EMAIL_RECEIVERS', '').split(',') if r.strip()],
            stock_email_groups=cls._parse_stock_email_groups(),
            pushover_user_key=os.getenv('PUSHOVER_USER_KEY'),
            pushover_api_token=os.getenv('PUSHOVER_API_TOKEN'),
            pushplus_token=os.getenv('PUSHPLUS_TOKEN'),
            pushplus_topic=os.getenv('PUSHPLUS_TOPIC'),
            serverchan3_sendkey=os.getenv('SERVERCHAN3_SENDKEY'),
            custom_webhook_urls=[u.strip() for u in os.getenv('CUSTOM_WEBHOOK_URLS', '').split(',') if u.strip()],
            custom_webhook_bearer_token=os.getenv('CUSTOM_WEBHOOK_BEARER_TOKEN'),
            webhook_verify_ssl=os.getenv('WEBHOOK_VERIFY_SSL', 'true').lower() == 'true',
            discord_bot_token=os.getenv('DISCORD_BOT_TOKEN'),
            discord_main_channel_id=(
                os.getenv('DISCORD_MAIN_CHANNEL_ID')
                or os.getenv('DISCORD_CHANNEL_ID')
            ),
            discord_webhook_url=os.getenv('DISCORD_WEBHOOK_URL'),
            astrbot_url=os.getenv('ASTRBOT_URL'),
            astrbot_token=os.getenv('ASTRBOT_TOKEN'),
            single_stock_notify=os.getenv('SINGLE_STOCK_NOTIFY', 'false').lower() == 'true',
            report_type=cls._parse_report_type(os.getenv('REPORT_TYPE', 'simple')),
            report_summary_only=os.getenv('REPORT_SUMMARY_ONLY', 'false').lower() == 'true',
            report_templates_dir=os.getenv('REPORT_TEMPLATES_DIR', 'templates'),
            report_renderer_enabled=os.getenv('REPORT_RENDERER_ENABLED', 'false').lower() == 'true',
            report_integrity_enabled=os.getenv('REPORT_INTEGRITY_ENABLED', 'true').lower() == 'true',
            report_integrity_retry=int(os.getenv('REPORT_INTEGRITY_RETRY', '1')),
            report_history_compare_n=int(os.getenv('REPORT_HISTORY_COMPARE_N', '0')),
            analysis_delay=float(os.getenv('ANALYSIS_DELAY', '0')),
            gemini_request_delay=float(os.getenv('GEMINI_REQUEST_DELAY', '0')),
            merge_email_notification=os.getenv('MERGE_EMAIL_NOTIFICATION', 'false').lower() == 'true',
            feishu_max_bytes=int(os.getenv('FEISHU_MAX_BYTES', '20000')),
            wechat_max_bytes=wechat_max_bytes,
            wechat_msg_type=wechat_msg_type_lower,
            discord_max_words=int(os.getenv('DISCORD_MAX_WORDS', '2000')),
            markdown_to_image_channels=[
                c.strip().lower()
                for c in os.getenv('MARKDOWN_TO_IMAGE_CHANNELS', '').split(',')
                if c.strip()
            ],
            markdown_to_image_max_chars=int(os.getenv('MARKDOWN_TO_IMAGE_MAX_CHARS', '15000')),
            md2img_engine=cls._parse_md2img_engine(os.getenv('MD2IMG_ENGINE', 'wkhtmltoimage')),
            prefetch_realtime_quotes=os.getenv('PREFETCH_REALTIME_QUOTES', 'true').lower() == 'true',
            log_dir=os.getenv('LOG_DIR', './logs'),
            log_level=os.getenv('LOG_LEVEL', 'INFO'),
            max_workers=int(os.getenv('MAX_WORKERS', '3')),
            debug=os.getenv('DEBUG', 'false').lower() == 'true',
            config_validate_mode=os.getenv('CONFIG_VALIDATE_MODE', 'warn').lower(),
            http_proxy=os.getenv('HTTP_PROXY'),
            https_proxy=os.getenv('HTTPS_PROXY'),
            schedule_enabled=os.getenv('SCHEDULE_ENABLED', 'false').lower() == 'true',
            schedule_time=os.getenv('SCHEDULE_TIME', '18:00'),
            schedule_run_immediately=os.getenv('SCHEDULE_RUN_IMMEDIATELY', 'true').lower() == 'true',
            run_immediately=os.getenv('RUN_IMMEDIATELY', 'true').lower() == 'true',
            webui_enabled=os.getenv('WEBUI_ENABLED', 'false').lower() == 'true',
            webui_host=os.getenv('WEBUI_HOST', '127.0.0.1'),
            webui_port=int(os.getenv('WEBUI_PORT', '8000')),
            # 机器人配置
            bot_enabled=os.getenv('BOT_ENABLED', 'true').lower() == 'true',
            bot_command_prefix=os.getenv('BOT_COMMAND_PREFIX', '/'),
            bot_rate_limit_requests=int(os.getenv('BOT_RATE_LIMIT_REQUESTS', '10')),
            bot_rate_limit_window=int(os.getenv('BOT_RATE_LIMIT_WINDOW', '60')),
            bot_admin_users=[u.strip() for u in os.getenv('BOT_ADMIN_USERS', '').split(',') if u.strip()],
            # 飞书机器人
            feishu_verification_token=os.getenv('FEISHU_VERIFICATION_TOKEN'),
            feishu_encrypt_key=os.getenv('FEISHU_ENCRYPT_KEY'),
            feishu_stream_enabled=os.getenv('FEISHU_STREAM_ENABLED', 'false').lower() == 'true',
            # 钉钉机器人
            dingtalk_app_key=os.getenv('DINGTALK_APP_KEY'),
            dingtalk_app_secret=os.getenv('DINGTALK_APP_SECRET'),
            dingtalk_stream_enabled=os.getenv('DINGTALK_STREAM_ENABLED', 'false').lower() == 'true',
            # 企业微信机器人
            wecom_corpid=os.getenv('WECOM_CORPID'),
            wecom_token=os.getenv('WECOM_TOKEN'),
            wecom_encoding_aes_key=os.getenv('WECOM_ENCODING_AES_KEY'),
            wecom_agent_id=os.getenv('WECOM_AGENT_ID'),
            # Telegram
            telegram_webhook_secret=os.getenv('TELEGRAM_WEBHOOK_SECRET'),
            # Discord 机器人扩展配置
            discord_bot_status=os.getenv('DISCORD_BOT_STATUS', 'A股智能分析 | /help'),
            # 实时行情增强数据配置
            enable_realtime_quote=os.getenv('ENABLE_REALTIME_QUOTE', 'true').lower() == 'true',
            enable_realtime_technical_indicators=os.getenv(
                'ENABLE_REALTIME_TECHNICAL_INDICATORS', 'true'
            ).lower() == 'true',
            enable_chip_distribution=os.getenv('ENABLE_CHIP_DISTRIBUTION', 'true').lower() == 'true',
            # 实时行情数据源优先级：
            # - tencent: 腾讯财经，有量比/换手率/PE/PB等，单股查询稳定（推荐）
            # - akshare_sina: 新浪财经，基本行情稳定，但无量比
            # - efinance/akshare_em: 东财全量接口，数据最全但容易被封
            # - tushare: Tushare Pro，需要2000积分，数据全面
            realtime_source_priority=cls._resolve_realtime_source_priority(),
            realtime_cache_ttl=int(os.getenv('REALTIME_CACHE_TTL', '600')),
            circuit_breaker_cooldown=int(os.getenv('CIRCUIT_BREAKER_COOLDOWN', '300')),
            enable_fundamental_pipeline=os.getenv('ENABLE_FUNDAMENTAL_PIPELINE', 'true').lower() == 'true',
            fundamental_stage_timeout_seconds=float(
                os.getenv('FUNDAMENTAL_STAGE_TIMEOUT_SECONDS', '1.5')
            ),
            fundamental_fetch_timeout_seconds=float(
                os.getenv('FUNDAMENTAL_FETCH_TIMEOUT_SECONDS', '0.8')
            ),
            fundamental_retry_max=int(os.getenv('FUNDAMENTAL_RETRY_MAX', '1')),
            fundamental_cache_ttl_seconds=int(os.getenv('FUNDAMENTAL_CACHE_TTL_SECONDS', '120')),
            fundamental_cache_max_entries=int(os.getenv('FUNDAMENTAL_CACHE_MAX_ENTRIES', '256')),
            portfolio_risk_concentration_alert_pct=float(
                os.getenv('PORTFOLIO_RISK_CONCENTRATION_ALERT_PCT', '35.0')
            ),
            portfolio_risk_drawdown_alert_pct=float(
                os.getenv('PORTFOLIO_RISK_DRAWDOWN_ALERT_PCT', '15.0')
            ),
            portfolio_risk_stop_loss_alert_pct=float(
                os.getenv('PORTFOLIO_RISK_STOP_LOSS_ALERT_PCT', '10.0')
            ),
            portfolio_risk_stop_loss_near_ratio=float(
                os.getenv('PORTFOLIO_RISK_STOP_LOSS_NEAR_RATIO', '0.8')
            ),
            portfolio_risk_lookback_days=int(os.getenv('PORTFOLIO_RISK_LOOKBACK_DAYS', '180')),
            portfolio_fx_update_enabled=os.getenv('PORTFOLIO_FX_UPDATE_ENABLED', 'true').lower() == 'true',
            mx_apikey=mx_apikey,
            smart_screen_keyword=smart_screen_keyword
        )
    
    @classmethod
    def _parse_litellm_yaml(cls, config_path: str) -> List[Dict[str, Any]]:
        """Parse a standard LiteLLM config YAML file into Router model_list.

        Supports the ``os.environ/VAR_NAME`` syntax for secret references.
        Returns an empty list on any error (logged, never raises).
        """
        import logging
        _logger = logging.getLogger(__name__)
        try:
            import yaml
        except ImportError:
            _logger.warning("PyYAML not installed; LITELLM_CONFIG ignored. Install with: pip install pyyaml")
            return []

        path = Path(config_path)
        if not path.is_absolute():
            path = Path(__file__).parent.parent / path
        if not path.exists():
            _logger.warning(f"LITELLM_CONFIG file not found: {path}")
            return []

        try:
            with open(path, encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
        except Exception as e:
            _logger.warning(f"Failed to parse LITELLM_CONFIG: {e}")
            return []

        model_list = yaml_config.get('model_list', [])
        if not isinstance(model_list, list):
            _logger.warning("LITELLM_CONFIG: model_list must be a list")
            return []

        _logger.info("-" * 40)

        # Process model_list to resolve os.environ/VAR_NAME syntax and validate entries
        processed_list: List[Dict[str, Any]] = []
        for entry in model_list:
            if not isinstance(entry, dict):
                continue
            # Deep copy to avoid mutating original
            processed_entry = dict(entry)
            litellm_params = dict(processed_entry.get('litellm_params', {}))

            # Resolve os.environ/VAR_NAME syntax for api_key
            api_key = litellm_params.get('api_key', '')
            if isinstance(api_key, str) and api_key.startswith('os.environ/'):
                env_var = api_key.split('/', 1)[1]
                litellm_params['api_key'] = os.getenv(env_var, '')

            # Resolve os.environ/VAR_NAME syntax for api_base
            api_base = litellm_params.get('api_base', '')
            if isinstance(api_base, str) and api_base.startswith('os.environ/'):
                env_var = api_base.split('/', 1)[1]
                litellm_params['api_base'] = os.getenv(env_var, '')

            processed_entry['litellm_params'] = litellm_params
            processed_list.append(processed_entry)

        _logger.info(f"LITELLM_CONFIG: loaded {len(processed_list)} model deployment(s)")
        return processed_list

    @classmethod
    def _parse_llm_channels(cls, channels_str: str) -> List[Dict[str, Any]]:
        """Parse LLM_CHANNELS env var and per-channel env vars.

        Format:
            LLM_CHANNELS=deepseek,gemini
            LLM_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
            LLM_DEEPSEEK_API_KEY=sk-xxx
            LLM_DEEPSEEK_MODELS=deepseek-chat
        """
        import logging
        _logger = logging.getLogger(__name__)

        channels: List[Dict[str, Any]] = []
        for raw_name in channels_str.split(','):
            ch_name = raw_name.strip()
            if not ch_name:
                continue
            ch_upper = ch_name.upper()

            # 读取渠道配置
            base_url = os.getenv(f'LLM_{ch_upper}_BASE_URL', '').strip() or None
            enabled = parse_env_bool(os.getenv(f'LLM_{ch_upper}_ENABLED'), default=True)

            # 读取 API keys（优先使用 API_KEYS，其次 API_KEY）
            api_keys_raw = os.getenv(f'LLM_{ch_upper}_API_KEYS', '')
            api_keys = [k.strip() for k in api_keys_raw.split(',') if k.strip()]
            if not api_keys:
                single_key = os.getenv(f'LLM_{ch_upper}_API_KEY', '').strip()
                if single_key:
                    api_keys = [single_key]

            # 读取模型列表
            models_raw = os.getenv(f'LLM_{ch_upper}_MODELS', '')
            raw_models = [m.strip() for m in models_raw.split(',') if m.strip()]

            # 确定协议
            protocol = ch_name.lower()

            # 标准化模型名称
            models = []
            for model in raw_models:
                models.append(model) # 直接添加模型名称，不进行任何处理，要求必须写正确格式

            # 跳过禁用的渠道
            if not enabled:
                _logger.info(f"LLM channel '{ch_name}': disabled, skipped")
                continue

            # 跳过缺少 API key 或模型的渠道
            if not api_keys:
                _logger.warning(f"LLM channel '{ch_name}': no API key configured, skipped")
                continue
            if not models:
                _logger.warning(f"LLM channel '{ch_name}': no models configured, skipped")
                continue

            # 添加渠道配置
            channels.append({
                'name': ch_name.lower(),
                'protocol': protocol,
                'enabled': enabled,
                'base_url': base_url,
                'api_keys': api_keys,
                'models': models,
                'extra_headers': None,
            })
            _logger.info(f"LLM channel '{ch_name}': {len(models)} model(s), {len(api_keys)} key(s)")

        return channels

    @classmethod
    def _channels_to_model_list(cls, channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert parsed LLM channels to LiteLLM Router model_list format."""
        model_list: List[Dict[str, Any]] = []
        for ch in channels:
            for model_name in ch['models']:
                for api_key in ch['api_keys']:
                    litellm_params: Dict[str, Any] = {
                        'model': model_name,
                    }
                    if api_key:
                        litellm_params['api_key'] = api_key
                    if ch['base_url']:
                        litellm_params['api_base'] = ch['base_url']
                    # 添加自定义 headers（如果有）
                    headers = dict(ch.get('extra_headers') or {})
                    if headers:
                        litellm_params['extra_headers'] = headers

                    model_list.append({
                        'model_name': model_name,
                        'litellm_params': litellm_params,
                    })
        return model_list

    @classmethod
    def _parse_stock_email_groups(cls) -> List[Tuple[List[str], List[str]]]:
        """
        Parse STOCK_GROUP_N and EMAIL_GROUP_N from environment.
        Returns [(stocks, emails), ...] ordered by group index.
        """
        groups: dict = {}
        stock_re = re.compile(r'^STOCK_GROUP_(\d+)$', re.IGNORECASE)
        email_re = re.compile(r'^EMAIL_GROUP_(\d+)$', re.IGNORECASE)
        for key in os.environ:
            m = stock_re.match(key)
            if m:
                idx = int(m.group(1))
                val = os.environ[key].strip()
                groups.setdefault(idx, {})['stocks'] = [c.strip() for c in val.split(',') if c.strip()]
            m = email_re.match(key)
            if m:
                idx = int(m.group(1))
                val = os.environ[key].strip()
                groups.setdefault(idx, {})['emails'] = [e.strip() for e in val.split(',') if e.strip()]
        result = []
        for idx in sorted(groups.keys()):
            g = groups[idx]
            if 'stocks' in g and 'emails' in g and g['stocks'] and g['emails']:
                result.append((g['stocks'], g['emails']))
        return result

    @classmethod
    def _parse_report_type(cls, value: str) -> str:
        """Parse REPORT_TYPE, fallback to simple for invalid values (supports brief)."""
        v = (value or 'simple').strip().lower()
        if v in ('simple', 'full', 'brief'):
            return v
        import logging
        logging.getLogger(__name__).warning(
            f"REPORT_TYPE '{value}' invalid, fallback to 'simple' (valid: simple/full/brief)"
        )
        return 'simple'

    @classmethod
    def _parse_news_strategy_profile(cls, value: Optional[str]) -> str:
        """Parse NEWS_STRATEGY_PROFILE, fallback to short for invalid values."""
        normalized = normalize_news_strategy_profile(value)
        raw = (value or "short").strip().lower()
        if raw != normalized:
            logging.getLogger(__name__).warning(
                "NEWS_STRATEGY_PROFILE '%s' invalid, fallback to 'short' "
                "(valid: ultra_short/short/medium/long)",
                value,
            )
        return normalized

    def get_effective_news_window_days(self) -> int:
        """Return effective news window days after profile + max-age merge."""
        return resolve_news_window_days(
            news_max_age_days=self.news_max_age_days,
            news_strategy_profile=self.news_strategy_profile,
        )

    @classmethod
    def _parse_md2img_engine(cls, value: str) -> str:
        """Parse MD2IMG_ENGINE, fallback to wkhtmltoimage for invalid values (Issue #455)."""
        v = (value or 'wkhtmltoimage').strip().lower()
        if v in ('wkhtmltoimage', 'markdown-to-file'):
            return v
        if v:
            import logging
            logging.getLogger(__name__).warning(
                f"MD2IMG_ENGINE '{value}' invalid, fallback to 'wkhtmltoimage' "
                "(valid: wkhtmltoimage | markdown-to-file)"
            )
        return 'wkhtmltoimage'

    @classmethod
    def _resolve_realtime_source_priority(cls) -> str:
        """
        Resolve realtime source priority with automatic tushare injection.

        When TUSHARE_TOKEN is configured but REALTIME_SOURCE_PRIORITY is not
        explicitly set, automatically prepend 'tushare' to the default priority
        so that the paid data source is utilized for realtime quotes as well.
        """
        explicit = os.getenv('REALTIME_SOURCE_PRIORITY')
        default_priority = 'tencent,akshare_sina,efinance,akshare_em'

        if explicit:
            # User explicitly set priority, respect it
            return explicit

        tushare_token = os.getenv('TUSHARE_TOKEN', '').strip()
        if tushare_token:
            # Token configured but no explicit priority override
            # Prepend tushare so the paid source is tried first
            import logging
            logger = logging.getLogger(__name__)
            resolved = f'tushare,{default_priority}'
            logger.info(
                f"TUSHARE_TOKEN detected, auto-injecting tushare into realtime priority: {resolved}"
            )
            return resolved

        return default_priority

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（主要用于测试）"""
        cls._instance = None

    def is_agent_available(self) -> bool:
        """Check whether agent capabilities are usable.

        Decision table:

        +-----------------------+-------------------+---------+
        | AGENT_MODE env        | LITELLM_MODEL set | Result  |
        +-----------------------+-------------------+---------+
        | ``true``              | any               | True    |
        | ``false`` (explicit)  | any               | False   |
        | not set (default)     | yes               | True    |
        | not set (default)     | no                | False   |
        +-----------------------+-------------------+---------+

        This keeps backward compatibility: users who never touch
        ``AGENT_MODE`` get agent features automatically once they configure
        a model, while ``AGENT_MODE=false`` acts as an explicit kill-switch.
        """
        # Explicit AGENT_MODE takes full precedence
        if self._agent_mode_explicit:
            return self.agent_mode
        # Auto-detect: if LITELLM_MODEL is set, agent is implicitly available
        if self.litellm_model:
            return True
        return False

    def refresh_stock_list(self) -> None:
        """
        热读取 STOCK_LIST 环境变量并更新配置中的自选股列表
        
        支持两种配置方式：
        1. .env 文件（本地开发、定时任务模式） - 修改后下次执行自动生效
        2. 系统环境变量（GitHub Actions、Docker） - 启动时固定，运行中不变
        """
        # 优先从 .env 文件读取最新配置，这样即使在容器环境中修改了 .env 文件，
        # 也能获取到最新的股票列表配置
        env_file = os.getenv("ENV_FILE")
        env_path = Path(env_file) if env_file else (Path(__file__).parent.parent / '.env')
        stock_list_str = ''
        if env_path.exists():
            # 直接从 .env 文件读取最新的配置
            env_values = dotenv_values(env_path)
            stock_list_str = (env_values.get('STOCK_LIST') or '').strip()

        # 如果 .env 文件不存在或未配置，才尝试从系统环境变量读取
        if not stock_list_str:
            stock_list_str = os.getenv('STOCK_LIST', '')

        stock_list = [
            (c or "").strip().upper()
            for c in stock_list_str.split(',')
            if (c or "").strip()
        ]

        if not stock_list:
            stock_list = ['000001']

        self.stock_list = stock_list
    
    def validate_structured(self) -> List[ConfigIssue]:
        """Return structured validation issues with severity levels.

        Covers all three LLM configuration tiers introduced by PR #494:
        - LITELLM_CONFIG (YAML)
        - LLM_CHANNELS (env)
        - Legacy per-provider keys

        Returns:
            List of ConfigIssue objects, each carrying a severity
            ("error" | "warning" | "info"), a human-readable message, and the
            primary environment variable / field name it relates to.
        """
        issues: List[ConfigIssue] = []

        # --- Stock list ---
        if not self.stock_list:
            issues.append(ConfigIssue(
                severity="error",
                message="未配置自选股列表 (STOCK_LIST)",
                field="STOCK_LIST",
            ))

        # --- Data sources (informational only) ---
        if not self.tushare_token:
            issues.append(ConfigIssue(
                severity="info",
                message="未配置 Tushare Token，将使用其他数据源",
                field="TUSHARE_TOKEN",
            ))

        # --- LLM availability ---
        # 简化版配置：直接使用 GEMINI_API_KEY / DEEPSEEK_API_KEY
        has_simple_config = bool(
            os.getenv('GEMINI_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
        )
        
        if not has_simple_config and not self.gemini_api_keys and not self.deepseek_api_keys:
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    "未配置任何 LLM API Key。\n"
                    "请配置以下环境变量之一：\n"
                    "  - GEMINI_API_KEY（推荐，主模型）\n"
                    "  - DEEPSEEK_API_KEY（可选，作为 fallback）"
                ),
                field="GEMINI_API_KEY",
            ))
        # 简化版配置：不再检查 LITELLM_MODEL、fallback 和 vision 配置

        # --- Search engine (informational only) ---
        if not (
            self.bocha_api_keys
            or self.minimax_api_keys
            or self.tavily_api_keys
            or self.brave_api_keys
            or self.searxng_base_urls
        ):
            issues.append(ConfigIssue(
                severity="info",
                message="未配置搜索引擎 API Key (Bocha/MiniMax/Tavily/Brave/SearXNG)，新闻搜索功能将不可用",
                field="BOCHA_API_KEY",
            ))

        # --- Notification channels ---
        has_notification = bool(
            self.wechat_webhook_url
            or self.feishu_webhook_url
            or (self.telegram_bot_token and self.telegram_chat_id)
            or (self.email_sender and self.email_password)
            or (self.pushover_user_key and self.pushover_api_token)
            or self.pushplus_token
            or self.serverchan3_sendkey
            or self.custom_webhook_urls
            or (self.discord_bot_token and self.discord_main_channel_id)
            or self.discord_webhook_url
        )

        if not has_notification:
            issues.append(ConfigIssue(
                severity="warning",
                message="未配置通知渠道，将不发送推送通知",
                field="WECHAT_WEBHOOK_URL",
            ))

        # --- Deprecated field migration hints ---
        if os.getenv("OPENAI_VISION_MODEL"):
            issues.append(ConfigIssue(
                severity="info",
                message=(
                    "OPENAI_VISION_MODEL 已废弃，请改用 VISION_MODEL。"
                    "当前值已自动迁移，建议更新配置文件以消除此提示。"
                ),
                field="OPENAI_VISION_MODEL",
            ))

        # --- Vision key availability ---
        # Only warn when user explicitly set VISION_MODEL (or OPENAI_VISION_MODEL alias).
        # Skipped when vision_model is empty (Vision not intentionally configured).
        if self.vision_model:
            # Maps provider prefix → the corresponding key list tracked by Config.
            # vertex_ai shares gemini keys; other LiteLLM-native providers are not
            # in this map (their keys come from env vars, which we cannot inspect here).
            _VISION_KEY_MAP = {
                "gemini": self.gemini_api_keys,
                "vertex_ai": self.gemini_api_keys,
                "deepseek": self.deepseek_api_keys,
            }
            # Derive the primary model's provider prefix so that its key is also
            # checked even when the provider is absent from VISION_PROVIDER_PRIORITY.
            _primary_prefix = (
                self.vision_model.split("/")[0]
                if "/" in self.vision_model
                else "openai"
            )
            _priority_providers = [
                p.strip().lower()
                for p in self.vision_provider_priority.split(",")
                if p.strip()
            ]
            # Union: fallback providers + primary model's own provider
            _all_providers = {_primary_prefix} | set(_priority_providers)

            # Align with get_api_keys_for_model: keys must be non-empty and len >= 8
            _has_any_key = any(
                any(k and len(k) >= 8 for k in (_VISION_KEY_MAP.get(p) or []))
                for p in _all_providers
                if p in _VISION_KEY_MAP
            )
            if not _has_any_key:
                _checked = sorted(_all_providers & _VISION_KEY_MAP.keys())
                issues.append(ConfigIssue(
                    severity="warning",
                    message=(
                        "VISION_MODEL 已配置，但未找到可用的 Vision API Key "
                        f"（已检查：{', '.join(_checked)}）。"
                        "图片股票代码提取功能将不可用，请配置对应的 API Key。"
                    ),
                    field="VISION_MODEL",
                ))

        return issues

    def validate(self) -> List[str]:
        """Return validation messages as plain strings (backward-compatible).

        Internally delegates to validate_structured().  Callers that only need
        the human-readable strings can continue to use this method unchanged.

        Returns:
            List of message strings, one per ConfigIssue.
        """
        return [issue.message for issue in self.validate_structured()]


# === 便捷的配置访问函数 ===
def get_config() -> Config:
    """获取全局配置实例的快捷方式"""
    return Config.get_instance()

