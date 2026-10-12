"""文本生成、流式响应、语音合成与 OpenAI 兼容聊天接口路由。

从原 main.py 剥离，专注于 LLM / 语音链路的处理逻辑。
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import time
from typing import Any, AsyncIterator, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import features
from ..nai import UpstreamError, _wait_cleanup
from ..sse import encode_sse
from ..policy import clamp_text_params, estimate_tokens
from ..upstream_errors import text_stream_events, upstream_error_message

TEXT_MODELS = [
    "llama-3-erato-v1", "kayra-v1", "clio-v1",
    "nai-glm-4-6", "nai-xialong",
]


def _main():
    from .. import main
    return main


class TextStreamResponse(StreamingResponse):
    """Transfer the route's upstream/slot ownership to the ASGI response."""

    def __init__(self, content, resources: AsyncExitStack, **kwargs):
        super().__init__(content, **kwargs)
        self.resources = resources.pop_all()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            async def cleanup():
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.resources.aclose()

            await _wait_cleanup(asyncio.create_task(cleanup()))


async def generate_stream(request: Request):
    main = _main()
    key = await main.authenticate(request)
    await main.check_rpm(key)
    await main.require_feature(key, "text")
    body = await main.read_json(request, limit_mb=main.TEXT_BODY_MB)
    model = str(body.get("model", "?"))
    main._require_text_model(key, model, "text")

    body, notes, problem = clamp_text_params(
        body,
        max_output_tokens=main.STATE.settings.max_text_output_tokens,
        max_input_chars=main.STATE.settings.max_input_chars,
    )
    if problem:
        main.record(key, "text", model, "rejected", detail=problem)
        raise main.err(400, problem)
    await main._text_quota_check(key, body)

    daily_limit = -1 if key["is_admin"] else key["daily_text_tokens"]

    async with AsyncExitStack() as resources:
        await resources.enter_async_context(main.acquire_concurrency(key))
        already = (await main.STATE.db.get_counter(key["id"], main.STATE.day()))["text_tokens"]
        try:
            resp = await main.STATE.nai.stream(main._text_url(model, True), body)
        except UpstreamError as e:
            main.record(key, "text", model, "error", detail=e.message)
            raise main.err(e.status if e.status in (400, 422, 429, 503) else 502, e.message)
        resources.push_async_callback(resp.aclose)

        async def passthrough() -> AsyncIterator[bytes]:
            counted = 0
            hard_cut = False
            try:
                async for raw, event, payload in text_stream_events(resp):
                    if payload is not None:
                        counted += 1
                        if 0 <= daily_limit <= already + counted:
                            hard_cut = True
                            yield b"data: [DONE]\n\n"
                            return
                    yield encode_sse(raw, event)
            except UpstreamError as exc:
                main.record(key, "text", model, "error", detail=exc.message)
                yield encode_sse(json.dumps({"error": exc.message, "message": exc.message,
                                             "status_code": exc.status}, ensure_ascii=False).encode(), b"error")
            finally:
                d = ("达到每日上限被截断; " if hard_cut else "") + "; ".join(notes)
                if counted:
                    await main.settle_record(key, "text", model, "ok", tokens=counted, detail=d.strip("; "))

        return TextStreamResponse(passthrough(), resources, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})


async def generate_text(request: Request):
    main = _main()
    key = await main.authenticate(request)
    await main.check_rpm(key)
    await main.require_feature(key, "text")
    body = await main.read_json(request, limit_mb=main.TEXT_BODY_MB)
    model = str(body.get("model", "?"))
    main._require_text_model(key, model, "text")

    body, notes, problem = clamp_text_params(
        body,
        max_output_tokens=main.STATE.settings.max_text_output_tokens,
        max_input_chars=main.STATE.settings.max_input_chars,
    )
    if problem:
        main.record(key, "text", model, "rejected", detail=problem)
        raise main.err(400, problem)
    await main._text_quota_check(key, body)

    async with main.acquire_concurrency(key):
        resp = await main.upstream_call(main._text_url(model, False), body, accept="application/json")
    if resp.status_code != 200:
        main.record(key, "text", model, "error", detail=f"upstream {resp.status_code}")
        raise main.err(resp.status_code, upstream_error_message(resp.status_code))
    try:
        out = (resp.json() or {}).get("output", "")
    except Exception:
        out = ""
    main.record(key, "text", model, "ok", tokens=estimate_tokens(out), detail="; ".join(notes))
    return Response(resp.content, media_type="application/json")


async def generate_voice(request: Request):
    main = _main()
    key = await main.authenticate(request)
    await main.check_rpm(key)
    await main.require_feature(key, "voice")
    if not key["is_admin"]:
        main.record(key, "voice", "", "rejected", detail="voice is admin-only")
        raise main.err(403, "语音合成目前仅限管理员 Key（它按 Anlas 计费，暂未接入额度统计）")
    body = await main.read_json(request, limit_mb=1)
    async with main.acquire_concurrency(key):
        resp = await main.upstream_call(f"{main.STATE.nai.legacy_text_host}/ai/generate-voice", body)
    if resp.status_code != 200:
        raise main.err(resp.status_code, upstream_error_message(resp.status_code))
    main.record(key, "voice", str(body.get("voice", "")), "ok")
    return Response(resp.content, media_type=resp.headers.get("content-type", "audio/mpeg"))


async def v1_models(request: Request):
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "novelai"} for m in TEXT_MODELS
    ]}


async def v1_me(request: Request):
    main = _main()
    key = await main.authenticate(request)
    c = await main.STATE.db.get_counter(key["id"], main.STATE.day())
    generated_images = await main.STATE.db.generated_image_totals(key["id"])
    return {
        "name": key["name"],
        "is_admin": bool(key["is_admin"]),
        "generated_images_total": generated_images.get(key["id"], 0),
        "today": {
            "images": c["images"], "daily_images": key["daily_images"],
            "daily_images_base": main.daily_base(key),
            "legacy_free_images_today": c["legacy_free_images"],
            "anlas_today": round(float(c["anlas"]), 2),
            "daily_anlas": key["daily_anlas"],
            "anlas_auto": main._anlas_auto(key) and bool(key["allow_anlas"]),
            "v5_today": c["v5"], "daily_v5": key["daily_v5"],
            "image_model_scope": key["image_model_scope"],
            "anlas_month": round(await main.STATE.db.month_anlas(key["id"], main.STATE.month()), 2),
            "monthly_anlas": key["monthly_anlas"],
            "text_tokens": c["text_tokens"], "daily_text_tokens": key["daily_text_tokens"],
            "requests": c["requests"],
        },
        "expires_at": key["expires_at"],
        "inactivity_delete_days": 0 if key["is_admin"] else main.STATE.settings.key_inactivity_delete_days,
        "features": await _feature_view(key),
    }


async def _feature_view(key) -> list[dict]:
    """该 Key 当前各项功能是否可用（已计入全局开关）。"""
    main = _main()
    view = []
    for name, label in features.FEATURES.items():
        on = await features.check(main.STATE.db, key, name, main.STATE.settings) is None
        if name == "voice" and not key["is_admin"]:
            on = False
        view.append({"id": name, "label": label, "on": on})
    return view


async def v1_chat(request: Request):
    main = _main()
    key = await main.authenticate(request)
    await main.check_rpm(key)
    await main.require_feature(key, "text")
    body = await main.read_json(request, limit_mb=main.TEXT_BODY_MB)
    want_stream = bool(body.get("stream"))

    msgs = body.get("messages") or []
    input_text = "\n".join(
        str(m.get("content", "")) for m in msgs if isinstance(m, dict)
    ).strip()
    model_in = str(body.get("model", ""))
    model = model_in if model_in in TEXT_MODELS else "llama-3-erato-v1"
    ml = int(body.get("max_tokens") or 150)
    payload = {
        "input": input_text,
        "model": model,
        "parameters": {
            "max_length": min(max(1, ml), main.STATE.settings.max_text_output_tokens),
            "min_length": 1,
            "temperature": float(body.get("temperature", 0.9) or 0.9),
            "top_p": float(body.get("top_p", 0.9) or 0.9),
            "top_k": 40,
            "use_string": True,
        },
    }
    nai_body, _, problem = clamp_text_params(
        payload,
        max_output_tokens=main.STATE.settings.max_text_output_tokens,
        max_input_chars=main.STATE.settings.max_input_chars,
    )
    if problem:
        raise main.err(400, problem)
    await main._text_quota_check(key, nai_body)

    daily_limit = -1 if key["is_admin"] else key["daily_text_tokens"]

    async with AsyncExitStack() as resources:
        await resources.enter_async_context(main.acquire_concurrency(key))
        already = (await main.STATE.db.get_counter(key["id"], main.STATE.day()))["text_tokens"]
        try:
            resp = await main.STATE.nai.stream(main._text_url(model, True), nai_body)
        except UpstreamError as e:
            main.record(key, "chat", model, "error", detail=e.message)
            raise main.err(e.status if e.status in (400, 422, 429, 503) else 502, e.message)
        resources.push_async_callback(resp.aclose)

        def openai_chunk(content: str, finish: Optional[str] = None) -> str:
            return json.dumps({
                "id": "chatcmpl-naigate", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": content} if content else {},
                             "finish_reason": finish}],
            }, ensure_ascii=False)

        result: dict[str, Any] = {"collected": [], "counted": 0}

        async def run_stream() -> AsyncIterator[bytes]:
            try:
                if want_stream:
                    yield ("data: " + openai_chunk("", None) + "\n\n").encode()
                async for _raw, _event, obj in text_stream_events(resp):
                    if obj is None:
                        break
                    text = obj["token"]
                    if not text:
                        continue
                    result["counted"] += 1
                    if 0 <= daily_limit <= already + result["counted"]:
                        break
                    if want_stream:
                        yield ("data: " + openai_chunk(text, None) + "\n\n").encode()
                    else:
                        result["collected"].append(text)
                if want_stream:
                    yield ("data: " + openai_chunk("", "stop") + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
            except UpstreamError as exc:
                main.record(key, "chat", model, "error", detail=exc.message)
                if not want_stream:
                    raise main.err(exc.status, exc.message) from None
                yield encode_sse(json.dumps({"error": {"message": exc.message, "status": exc.status}},
                                             ensure_ascii=False).encode())
                yield b"data: [DONE]\n\n"
            finally:
                if result["counted"]:
                    await main.settle_record(key, "chat", model, "ok", tokens=result["counted"])

        if want_stream:
            return TextStreamResponse(run_stream(), resources, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        async for _ in run_stream():
            pass
        content = "".join(result["collected"])
        counted = result["counted"]
        return JSONResponse({
            "id": "chatcmpl-naigate", "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": estimate_tokens(input_text),
                      "completion_tokens": max(counted, 1),
                      "total_tokens": estimate_tokens(input_text) + max(counted, 1)},
        })


def register_routes(app, limit_inflight):
    """注册文本与 OpenAI 兼容路由到主应用。"""
    for path in ("/ai/generate-stream", "/nai/ai/generate-stream"):
        app.post(path)(limit_inflight(generate_stream))
    for path in ("/ai/generate", "/nai/ai/generate"):
        app.post(path)(limit_inflight(generate_text))
    for path in ("/ai/generate-voice", "/nai/ai/generate-voice"):
        app.post(path)(generate_voice)
    app.get("/v1/models")(v1_models)
    app.get("/v1")(v1_models)
    app.post("/v1/chat/completions")(limit_inflight(v1_chat))
    app.get("/v1/me")(v1_me)
