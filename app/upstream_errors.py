"""Public errors describe the failure without including upstream response data."""
import json

import httpx

from .nai import UpstreamError
from .sse import SSEDecoder


def upstream_error_message(status: int) -> str:
    # Upstream JSON/HTML can contain account details, URLs or echoed credentials.
    # Select a local message from the status instead of attempting to redact it.
    messages = {
        400: "上游未接受请求参数，请检查输入",
        401: "上游账号认证失败，请联系站长",
        402: "上游账号余额或权限不足，请联系站长",
        403: "上游拒绝了请求，请联系站长",
        404: "上游接口或模型不可用，请联系站长",
        413: "请求内容过大，请缩小图片或减少输入",
        422: "上游未接受请求参数，请检查输入",
        429: "上游请求过于频繁，请稍后重试",
    }
    message = messages.get(status, "上游服务暂时不可用，请稍后重试" if status >= 500
                           else "上游请求失败，请联系站长")
    return f"{message}（上游状态 {status}）"


async def text_stream_events(response):
    decoder = SSEDecoder(event_limit=1024 * 1024, stream_limit=128 * 1024 * 1024)
    try:
        async for chunk in response.aiter_bytes():
            for raw, event in decoder.feed(chunk):
                if event == b"error":
                    raise UpstreamError(502, "上游文本生成失败，请稍后重试")
                if raw == b"[DONE]":
                    yield raw, event, None
                    return
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError()
                if "error" in payload or "message" in payload or payload.get("event_type") == "error":
                    raise UpstreamError(502, "上游文本生成失败，请稍后重试")
                if "token" not in payload:
                    continue
                if not isinstance(payload["token"], str):
                    raise ValueError()
                yield raw, event, payload
    except (ValueError, UnicodeError, RecursionError):
        raise UpstreamError(502, "上游文本事件流格式无效") from None
    except httpx.HTTPError:
        raise UpstreamError(502, "上游文本流连接中断，请稍后重试") from None
    finally:
        decoder.finish()
