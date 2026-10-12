"""每个请求的耗时与客户端信息：排队等待、上游生成耗时、客户端名称（User-Agent 摘要）。

用一个可变 dict 放在 ContextVar 里：即使发送上游的代码跑在子任务里（子任务复制的是上下文，
dict 本身是同一个对象），也能把“开始发送上游”的时间点写回来。
"""
from __future__ import annotations

import re
import secrets
import time
from contextvars import ContextVar
from typing import Optional

_TIMING: ContextVar[Optional[dict]] = ContextVar("gate_request_timing", default=None)
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]+")
CLIENT_MAX = 256       # 完整 User-Agent：前 60 个字符里 12 把 Windows Key 一模一样，区分不了客户端（统计审查）


def client_name(user_agent: str) -> str:
    """只保留 User-Agent 的前一段，去掉控制字符；它由客户端自己填写，仅作参考。"""
    text = _UNSAFE.sub(" ", user_agent or "").strip()
    return text[:CLIENT_MAX]


def begin(scope) -> object:
    ua = ""
    is_aalice = False
    for name, value in scope.get("headers") or ():
        if name == b"user-agent":
            ua = value.decode("latin-1", "replace")
        elif name in (b"x-correlation-id", b"x-initiated-at"):
            is_aalice = True

    cname = client_name(ua)
    if is_aalice or "dart" in ua.lower():
        if not cname.startswith("Aaalice"):
            cname = f"Aaalice_NAI_Launcher ({cname})"[:CLIENT_MAX]
    elif "dsh" in ua.lower():
        if not cname.startswith("DSH-Tavern"):
            cname = f"DSH-Tavern-Image ({cname})"[:CLIENT_MAX]
    elif "luker" in ua.lower():
        if not cname.startswith("Luker"):
            cname = f"Luker ({cname})"[:CLIENT_MAX]
    elif "tauri" in ua.lower():
        if not cname.startswith("Tauri"):
            cname = f"TauriTavern ({cname})"[:CLIENT_MAX]
    elif "workbench" in ua.lower():
        if not cname.startswith("NAI-Workbench"):
            cname = f"NAI-Workbench ({cname})"[:CLIENT_MAX]

    return _TIMING.set({"t0": time.monotonic(), "sent": None, "client": cname, "status": 0,
                        "rid": secrets.token_hex(4)})


def rid() -> str:
    """本请求的编号（8 位十六进制），写进响应头 X-Request-Id 和日志，成员报错时给站长看这个就能定位。"""
    holder = _TIMING.get()
    return holder["rid"] if holder is not None else ""


def end(token) -> None:
    _TIMING.reset(token)


def mark_logged() -> None:
    """本请求已经写过用量日志。放在共享 dict 里：子任务里记的日志外层也能看到（普通 ContextVar 布尔值传不回来，
    以前上游 4xx 会先在子任务里记一行 error、再被外层异常处理器补记一行 rejected）。"""
    holder = _TIMING.get()
    if holder is not None:
        holder["logged"] = True


def logged() -> bool:
    holder = _TIMING.get()
    return bool(holder is not None and holder.get("logged"))


def was_sent() -> bool:
    """本请求是否已经发往上游（发出过一次就算，重试不影响）。"""
    holder = _TIMING.get()
    return bool(holder is not None and holder.get("sent") is not None)


def mark_sent() -> None:
    """第一次真正把请求发往上游时调用；重试不覆盖。"""
    holder = _TIMING.get()
    if holder is not None and holder["sent"] is None:
        holder["sent"] = time.monotonic()
        hook = holder.get("on_sent")
        if hook is not None:
            hook()


def on_sent(hook) -> None:
    """登记一个回调：本请求第一次发往上游时调用（实时架构图把这张图从「排队」移到「生成中」）。"""
    holder = _TIMING.get()
    if holder is not None:
        holder["on_sent"] = hook


def mark_status(code: int) -> None:
    """记录上游最后一次返回的 HTTP 状态码（429 / 5xx 等），用于分析上游是否在限流或封控。"""
    holder = _TIMING.get()
    if holder is not None:
        holder["status"] = int(code)


def snapshot() -> dict:
    """wait_ms：收到请求到发往上游；dur_ms：上游耗时（没发到上游为 0）；up_status：上游状态码（没收到为 0）。"""
    holder = _TIMING.get()
    if holder is None:
        return {"wait_ms": 0, "dur_ms": 0, "client": "", "up_status": 0, "rid": "", "src": ""}
    now = time.monotonic()
    sent = holder["sent"]
    if sent is None:
        wait, dur = now - holder["t0"], 0.0
    else:
        wait, dur = sent - holder["t0"], now - sent
    return {"wait_ms": int(wait * 1000), "dur_ms": int(dur * 1000), "client": holder["client"],
            "up_status": holder["status"], "rid": holder.get("rid", ""), "src": holder.get("src", "")}


def set_model(model: str) -> None:
    """记下本请求的模型，让被拒的请求也能按模型统计（原来被拒行 model 为空）。"""
    holder = _TIMING.get()
    if holder is not None:
        holder["model"] = (model or "")[:80]


def model() -> str:
    holder = _TIMING.get()
    return holder.get("model", "") if holder is not None else ""


def set_source(label: str) -> None:
    """记下本请求的来源网络打码标签，写进用量日志（防分享溯源）。"""
    holder = _TIMING.get()
    if holder is not None:
        holder["src"] = label or ""


def refine_client_from_payload(body: dict) -> None:
    """对无明确自报 UA 的客户端，利用 Payload 参数指纹进行精准识别（如 dsh-tavern-image）。"""
    holder = _TIMING.get()
    if holder is None or not isinstance(body, dict):
        return
    current = holder.get("client", "")
    if current.startswith(("Aaalice", "Luker", "TauriTavern", "NAI-Workbench", "DSH-Tavern")):
        return

    params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    v4_prompt = params.get("v4_prompt") if isinstance(params.get("v4_prompt"), dict) else {}

    # dsh-tavern-image (weixinlll/dsh-tavern-image) 专有特征指纹
    if (
        body.get("action") == "generate"
        and params.get("params_version") == 3
        and params.get("ucPreset") == 3
        and v4_prompt.get("use_order") is True
        and v4_prompt.get("use_coords") is False
        and params.get("add_original_image") is True
        and params.get("controlnet_strength") == 1
    ):
        suffix = f" ({current})" if current else ""
        holder["client"] = f"DSH-Tavern-Image{suffix}"[:CLIENT_MAX]
