# -*- coding: utf-8 -*-
"""
Shared factory for building fully-configured AgentExecutor instances.

Centralises construction to eliminate boilerplate duplicated across
api/v1/endpoints/agent.py, bot/commands/chat.py, bot/commands/ask.py,
and src/core/pipeline.py.

Performance notes
-----------------
* ``ToolRegistry`` is built once and cached at module level — tool
  registrations are immutable after setup so the object is safe to share
  across every request.
* ``SkillManager`` is expensive to create (loads YAML files from disk).
  A prototype is built on first use and cheap ``deepcopy`` clones are
  returned for each request, preserving thread-safety (``activate()``
  mutates internal state).

Usage::

    from src.agent.factory import build_agent_executor

    executor = build_agent_executor(config, skills=["bull_trend", "shrink_pullback"])
    result   = executor.chat(message="...", session_id="...")
"""

import copy
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level caches
# ---------------------------------------------------------------------------
_TOOL_REGISTRY = None
_SKILL_MANAGER_PROTOTYPE = None
# Sentinel used as initial value so None (i.e. no custom dir) compares as "changed"
# on the very first call, forcing a build rather than accidentally skipping it.
_SENTINEL = object()
# Track which custom_dir the prototype was built with so we can invalidate
# the cache if AGENT_STRATEGY_DIR changes at runtime (e.g. via config reload).
_SKILL_MANAGER_CUSTOM_DIR: object = _SENTINEL

DEFAULT_AGENT_SKILLS = [
    "bull_trend",
    "ma_golden_cross",
    "volume_breakout",
    "shrink_pullback",
]



def get_skill_manager(config=None):
    """Return a deepcopy-clone of the cached SkillManager prototype.

    The prototype is initialised from disk on first call; subsequent calls
    return ``copy.deepcopy(prototype)`` which is ~10× faster than re-reading
    YAML files.  Each clone is independent so ``.activate()`` calls do not
    bleed between requests.

    Cache invalidation: if ``config.agent_strategy_dir`` changes at runtime
    (e.g. via the web settings reload), the prototype is rebuilt automatically.
    """
    global _SKILL_MANAGER_PROTOTYPE, _SKILL_MANAGER_CUSTOM_DIR

    if config is None:
        from src.config import get_config
        config = get_config()

    current_custom_dir = getattr(config, "agent_strategy_dir", None)
    if _SKILL_MANAGER_PROTOTYPE is not None and current_custom_dir == _SKILL_MANAGER_CUSTOM_DIR:
        return copy.deepcopy(_SKILL_MANAGER_PROTOTYPE)

    from src.agent.skills.base import SkillManager

    if _SKILL_MANAGER_PROTOTYPE is not None:
        logger.info("[AgentFactory] SkillManager prototype invalidated (agent_strategy_dir changed: %r → %r)",
                    _SKILL_MANAGER_CUSTOM_DIR, current_custom_dir)

    skill_manager = SkillManager()
    skill_manager.load_builtin_strategies()

    if current_custom_dir:
        try:
            skill_manager.load_custom_strategies(current_custom_dir)
        except Exception as exc:
            logger.warning("[AgentFactory] Failed to load custom strategies from %s: %s", current_custom_dir, exc)

    _SKILL_MANAGER_PROTOTYPE = skill_manager
    _SKILL_MANAGER_CUSTOM_DIR = current_custom_dir
    logger.info("[AgentFactory] SkillManager prototype cached (%d strategies)", len(skill_manager._skills))
    return copy.deepcopy(_SKILL_MANAGER_PROTOTYPE)
