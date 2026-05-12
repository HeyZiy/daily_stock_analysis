# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 异步任务队列
===================================

职责：
1. 管理异步分析任务的生命周期
2. 防止相同股票代码重复提交
3. 提供 SSE 事件广播机制
4. 任务完成后持久化到数据库
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Set, List, Callable, Any, TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from asyncio import Queue as AsyncQueue

from data_provider.base import canonical_stock_code

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"        # 等待执行
    PROCESSING = "processing"  # 执行中
    COMPLETED = "completed"    # 已完成
    FAILED = "failed"          # 失败


@dataclass
class TaskInfo:
    """
    任务信息数据类
    
    包含任务的完整状态信息，用于 API 响应和内部管理
    """
    task_id: str
    stock_code: str
    stock_name: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    report_type: str = "detailed"
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于 API 响应"""
        return {
            "task_id": self.task_id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "report_type": self.report_type,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }
    
    def copy(self) -> 'TaskInfo':
        """创建任务信息的副本"""
        return TaskInfo(
            task_id=self.task_id,
            stock_code=self.stock_code,
            stock_name=self.stock_name,
            status=self.status,
            progress=self.progress,
            message=self.message,
            result=self.result,
            error=self.error,
            report_type=self.report_type,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )

