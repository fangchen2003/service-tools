"""全局 GateState 注入与安全获取器。

消除子模块直接 import main.STATE 导致的循环引用与单体耦合。
"""
from __future__ import annotations

from typing import Optional
from starlette.requests import Request


def get_gate_state(request: Request):
    """从 FastAPI app.state 获取初始化的 GateState 实例。"""
    state = getattr(request.app.state, "gate", None)
    if state is None:
        # 兼容过渡期：如果 app.state.gate 尚未就绪，尝试从 main 模块获取
        from .. import main
        state = getattr(main, "STATE", None)
    return state
