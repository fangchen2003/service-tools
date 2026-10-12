"""请求生命周期上下文变量：全局单一真实源。

所有中间件、异常处理与路由模块统一由此处导入，保证 ContextVar 引用唯一且无模块循环导入。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

REQUEST_KEY: ContextVar[Optional[dict[str, Any]]] = ContextVar("gate_request_key", default=None)
REQUEST_LOGGED: ContextVar[bool] = ContextVar("gate_request_logged", default=False)
