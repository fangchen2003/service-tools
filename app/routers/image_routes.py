"""核心生图、流式生成、参考图(Vibe)、图片工具与标签建议路由及调度机制。

从原 main.py 剥离，聚合图片预算控制、并发排队、参数归一化及上游通信。
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import time
from urllib.parse import urlencode

import anyio
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
import httpx

from .. import quota_algo, reasons, request_timing, site_flags
from ..image_events import (
    ImageEventTracker,
    ImageStreamProtocolError,
    STREAM_MEDIA_TYPES,
)
from ..image_streaming import ImageStreamResponse
from ..image_tools import MAX_RESPONSE_BYTES, prepare_tool, validate_result
from ..image_payload import read_image_body
from ..nai import UpstreamError
from ..policy import (
    REFERENCE_FIELDS,
    clamp_image_params,
    estimate_image_cost,
    fill_v4_prompt,
    fix_name_digit_weights,
    image_model_tier,
    is_v5_medium,
    legacy_normal_free_eligible,
    medium_normalize,
    normalize_image_request,
    sanitize_and_translate_prompts,
    upstream_parameter_problem,
    validate_image_references,
    validate_vibe_encoding,
)
from ..upstream_errors import upstream_error_message

VIBE_ENCODING_ANLAS = 2


def _main():
    from .. import main
    return main


def _state():
    return _main().STATE


def _bug(*args, **kwargs):
    return _main().bug(*args, **kwargs)


async def read_image_payload(request: Request, limit_mb: float = 25) -> dict:
    """JSON 与 multipart 附件统一进入后续权限、参数和费用检查。"""
    try:
        return await read_image_body(request, int(limit_mb * 1024 * 1024))
    except HTTPException as exc:
        raise _main().err(exc.status_code, exc.detail) from None


def _anlas_auto(key) -> bool:
    """anlas_auto：1 = 自动分配管理；0 = 交给算法（暂未分到）；-1 = 站长手动管理（关闭或手动额度）。"""
    try:
        return key["anlas_auto"] == 1
    except (KeyError, IndexError, TypeError):
        return False


def _flag(key, name: str) -> bool:
    """读 Key 上可能不存在的新列（老测试数据 / 假对象）。"""
    try:
        return bool(key[name])
    except (KeyError, IndexError, TypeError):
        return False


def _manual(key) -> bool:
    """手动额度的 Key：填多少就是多少，不受保底 / 借用规则影响。"""
    try:
        return key["quota_auto"] == -1
    except (KeyError, IndexError, TypeError):
        return False


def daily_base(key) -> int:
    """这把 Key 的每日保底张数；0 表示没有保底 / 借用之分（整天都按每日上限）。"""
    guard = getattr(_state(), "guard", None)
    base = guard.values["base_daily_images"] if guard is not None else 0
    if key["is_admin"] or not base or not key["daily_images"] or base >= key["daily_images"] or _manual(key):
        return 0
    return base


async def guard_precheck() -> None:
    """出图前先看账号保护：所有可用账号都到了每日 / 每小时上限或安静时段上限时，直接 429 并说明原因。"""
    state = _state()
    guard = getattr(state, "guard", None)
    pool = [t for t in getattr(state.nai, "pool", []) if t.usable]
    if guard is None or not pool:
        return
    reasons_list = [await guard.token_block_reason(state.db, t.token_id, state.day()) for t in pool]
    if all(reasons_list):
        raise _main().err(429, reasons_list[0])


@asynccontextmanager
async def image_admission(key):
    """P1：每把 Key 同时生成 1 张、最多再排 N 张；全站排队总数有上限。超出直接 429，不进入排队。"""
    state = _state()
    guard = getattr(state, "guard", None)
    if guard is None or key["is_admin"]:
        yield
        return
    await guard_precheck()
    accounts = sum(1 for t in getattr(state.nai, "pool", []) if t.usable)
    reason = guard.admit_image(key["id"], accounts)
    if reason:
        raise _main().err(429, reason)
    entry_id = guard.entries[-1]["id"]           # admit_image 刚追加的那一条（中间没有 await，不会被别人插队）
    def _on_sent():
        waiting = guard.waiting_keys()           # 本张开始发往上游时，还在排队的其他 Key
        guard.mark_running(key["id"], entry_id)
        sched = getattr(state, "sched", None)
        if sched is not None:
            try:
                sched.observe_pick(key["id"], waiting)
                sched.on_serve(key["id"])
            except Exception as exc:
                _bug("scheduling", exc)
    request_timing.on_sent(_on_sent)
    try:
        yield
    finally:
        guard.release_image(key["id"], entry_id)


@asynccontextmanager
async def acquire_concurrency(key, *, image: bool = False):
    """Per-user admission; text also uses the legacy global request limit."""
    if image:
        async with image_admission(key), _acquire_slots(key, image=True):
            yield
    else:
        async with _acquire_slots(key, image=False):
            yield


@asynccontextmanager
async def _acquire_slots(key, *, image: bool):
    state = _state()
    t = state.settings.queue_timeout
    ksem = None if key["is_admin"] else state.key_sem(key["id"], state.settings.key_concurrency)
    async with AsyncExitStack() as resources:
        state.global_waiting += 1
        try:
            async with asyncio.timeout(t):
                if ksem is not None:
                    await resources.enter_async_context(ksem)
                if not image:
                    await resources.enter_async_context(state.global_sem)
        except TimeoutError:
            raise _main().err(429, "当前排队人数过多，请稍后再试")
        finally:
            state.global_waiting -= 1
        state.global_active += 1
        try:
            yield
        finally:
            state.global_active -= 1


async def quota_image_check(key, est: dict, *, legacy_free_images: int = 0,
                            exclude_reservation=None) -> None:
    if key["is_admin"]:
        return
    state = _state()
    main = _main()
    c = await state.db.get_counter(key["id"], state.day())
    pending = [r for r in getattr(state, "image_reservations", {}).values()
               if r is not exclude_reservation]
    own = [r for r in pending if r.key_id == key["id"]]
    # A request started before midnight can settle after midnight; count every
    # in-flight reservation conservatively against the current period.
    own_day = own_month = own
    global_day = global_month = pending
    if legacy_free_images and key["daily_images"] > 0:
        used_legacy = c["legacy_free_images"] + sum(r.legacy for r in own_day)
        if used_legacy + legacy_free_images > key["daily_images"]:
            raise main.err(402, f"今日 V4.5 及以下免费图额度已用完（{key['daily_images']} 张/天），明日恢复")
        # 平滑注水令牌桶机制：底仓 30 张，每小时自动注水恢复 4 张，全天恢复上限为 key['daily_images']。
        # 手动 Key（_manual(key) 为 True）不走注水限制，直接使用静态额度。
        if not _manual(key):
            try:
                now_ts = time.time()
                day_str = state.day() if callable(getattr(state, "day", None)) else getattr(state, "day", None)
                if not day_str:
                    day_str = time.strftime("%Y-%m-%d")
                midnight_ts = time.mktime(time.strptime(str(day_str), "%Y-%m-%d"))
                hours_today = max(0.0, (now_ts - midnight_ts) / 3600.0)
            except Exception:
                hours_today = 24.0
            burst_base = min(30.0, float(key["daily_images"]))
            refill_rate = 4.0  # 张/小时
            refilled_today = min(float(key["daily_images"]), burst_base + hours_today * refill_rate)
            if used_legacy + legacy_free_images > refilled_today:
                needed = (used_legacy + legacy_free_images) - burst_base
                needed_hours = needed / refill_rate
                wait_minutes = max(1, int(round((needed_hours - hours_today) * 60)))
                raise main.err(429, reasons.Reason(
                    f"当前额度蓄水池已用完（今日已用 {used_legacy} / 每日上限 {key['daily_images']} 张）。"
                    f"额度每小时自动恢复 4 张，约 {wait_minutes} 分钟后恢复 1 张，请稍后再试",
                    reasons.PACED
                ))
        # 保底与借用：超过每日保底后，只在全站空闲（没人排队、本小时用量不高）时放行，直到 Key 的每日上限。
        guard = getattr(state, "guard", None)
        base = guard.values["base_daily_images"] if guard is not None else 0
        if base and base < key["daily_images"] and used_legacy + legacy_free_images > base and not _manual(key):
            tokens = [t.token_id for t in getattr(state.nai, "pool", []) if t.usable]
            # 按时段放行时，超出保底的部分只能用「这个时段放出来、还剩得多」的份额：至少留半小时的量给保底内的人
            reserve = guard.values["account_daily_cap"] / 48 * len(tokens)
            if not guard.site_idle(key["id"], tokens):
                raise main.err(429, reasons.Reason(f"今天的保底 {base} 张已用完。全站空闲时可以继续用到 {key['daily_images']} 张，"
                                      "现在有其他人在排队或用量较高，请过几分钟再试", reasons.OVER_BASE))
            if await guard.paced_headroom(state.db, tokens, state.day()) <= reserve:
                # 文案不能含「其他人在排队」：每日复盘按它数 base_blocks（「有余量还被拦」），这里是份额紧（Sonnet 审查 P1）
                raise main.err(429, reasons.Reason(f"今天的保底 {base} 张已用完。这个时段放出的出图份额所剩不多，先留给还没用到保底的成员；"
                                      f"份额充裕时可以继续用到 {key['daily_images']} 张，请稍后再试", reasons.OVER_BASE))
    if est["v5"] > 0:
        # V5 周额度是账户级共享资源，用全站日计数镜像（恢复量 ~190 张/天）
        try:
            manual = key["quota_auto"] == -1
        except (KeyError, IndexError, TypeError):
            manual = False
        borrow = quota_algo.borrow_for(key["id"]) if key["daily_v5"] > 0 and not manual else 0    # 手动 Key 填多少就是多少，不借
        if key["daily_v5"] > 0 and c["v5"] + sum(r.v5 for r in own_day) + est["v5"] > key["daily_v5"] + borrow:
            raise main.err(402, f"已达今日 V5 额度（{key['daily_v5']} 张/天" + (f"，含空闲借用 {borrow} 张" if borrow else "")
                      + "），明天恢复后再用")
        g = await site_flags.get(state.db, site_flags.GLOBAL_DAILY_V5, state.settings)
        if g > 0 and quota_algo.BORROW.get("open"):
            g = int(g * (1 + quota_algo.BORROW_GLOBAL_RATIO))
        if g > 0 and not key["exclude_global_v5"]:
            total = await state.db.day_v5_total(state.day())
            if total + sum(r.v5 for r in global_day if not r.exclude_global_v5) + est["v5"] > g:
                raise main.err(402, f"全站今日 V5 额度已用完（{int(g)} 张/天），明天再来")
    if est["anlas"] > 0:
        is_vibe = bool(est.get("is_vibe"))
        until = await site_flags.get(state.db, site_flags.GLOBAL_DAILY_ANLAS_UNTIL, state.settings)
        if until > 0 and time.time() >= until:
            vibe_daily_cap = 0
        else:
            vibe_daily_cap = await site_flags.get(state.db, site_flags.GLOBAL_DAILY_ANLAS, state.settings)
        if is_vibe and not key["allow_anlas"]:
            if vibe_daily_cap <= 0:
                raise main.err(402, "该请求会消耗 Anlas，全站 Vibe 每日额度暂未开放")
            day_used = await state.db.day_anlas_total(state.day())
            in_flight = sum(r.anlas for r in global_day)
            if day_used + in_flight + est["anlas"] > vibe_daily_cap:
                raise main.err(402, f"全站今日 Vibe 额度已抢完（{int(vibe_daily_cap)} Anlas/天），明日 0 点恢复")
        elif not key["allow_anlas"]:
            raise main.err(402, "该请求会消耗 Anlas，此 Key 未开通付费额度权限")
        elif vibe_daily_cap > 0:
            day_used = await state.db.day_anlas_total(state.day())
            in_flight = sum(r.anlas for r in global_day)
            if day_used + in_flight + est["anlas"] > vibe_daily_cap:
                raise main.err(402, f"全站今日 Anlas 额度已抢完（{int(vibe_daily_cap)} Anlas/天），明日 0 点恢复")
        # 2026-10-10 站长：放开 Anlas。只用于 NovelAI 一定按 Anlas 收费的操作（超规格尺寸 / 步数、一次多张、
        # Vibe、放大、导演工具）：这些不占 V5 免费额度（官方 FAQ #38），也不影响 V4.5（Opus 不限）。每人仍受每日 Anlas 上限约束。
        if key["daily_anlas"] > 0 and c["anlas"] + sum(r.anlas for r in own_day) + est["anlas"] > key["daily_anlas"]:
            raise main.err(402, f"今日 Anlas 额度不足（已用 {c['anlas']:.0f} / 上限 "
                           f"{key['daily_anlas']:.0f}），明日恢复")
        used = await state.db.month_anlas(key["id"], state.month())
        if key["monthly_anlas"] > 0 and used + sum(r.anlas for r in own_month) + est["anlas"] > key["monthly_anlas"]:
            raise main.err(402, f"本月 Anlas 额度不足（已用 {used:.0f}/{key['monthly_anlas']:.0f}）")
        budget = await site_flags.get(state.db, site_flags.GLOBAL_MONTHLY_ANLAS, state.settings)
        if budget > 0:
            all_used = await state.db.month_anlas_all(state.month())
            if all_used + sum(r.anlas for r in global_month) + est["anlas"] > budget:
                raise main.err(402, f"全站本月 Anlas 预算已耗尽（{budget:.0f}），请联系站长")


class ImageReservation:
    def __init__(self, key, est, legacy):
        self.key_id = key["id"]
        self.exclude_global_v5 = bool(key["exclude_global_v5"])
        self.anlas = est["anlas"]
        self.v5 = est["v5"]
        self.legacy = legacy

    async def update(self, key, est, legacy=0):
        state = _state()
        async with state.image_budget_lock:
            await quota_image_check(key, est, legacy_free_images=legacy,
                                    exclude_reservation=self)
            self.anlas, self.v5, self.legacy = est["anlas"], est["v5"], legacy


@asynccontextmanager
async def reserve_image_budget(key, est, *, legacy_free_images=0):
    state = _state()
    main = _main()
    reservation = ImageReservation(key, est, legacy_free_images)
    try:
        async with asyncio.timeout(state.settings.queue_timeout):
            async with state.image_budget_lock:
                await quota_image_check(key, est, legacy_free_images=legacy_free_images)
                reservations = getattr(state, "image_reservations", None)
                if reservations is None:
                    reservations = state.image_reservations = {}
                reservations[id(reservation)] = reservation
                if hasattr(state, "image_budget_idle"):
                    state.image_budget_idle.clear()
    except (TimeoutError, main.GateError) as exc:
        # 尚未到达上游：归还用户 Key 的图片冷却占用，别让被拒的请求白占 15 秒。
        refund = getattr(state, "refund_key_image_slot", None)
        if refund is not None and not key["is_admin"]:
            await refund(key["id"])
        if isinstance(exc, TimeoutError):
            raise main.err(429, "图片预算核对排队超时，请稍后再试") from None
        raise
    try:
        yield reservation
    finally:
        with anyio.CancelScope(shield=True):
            async with state.image_budget_lock:
                state.image_reservations.pop(id(reservation), None)
                if not state.image_reservations and hasattr(state, "image_budget_idle"):
                    state.image_budget_idle.set()


async def complete_image_operation(operation, *, can_cancel=None, delivered=None):
    """Once sent, finish upstream response handling and its ledger even on disconnect."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(operation)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                if can_cancel is not None and can_cancel():
                    task.cancel()
        result = task.result()
    if cancelled and not (delivered is not None and delivered()):
        _bug("disconnect", title="客户端在图片返回前断开连接（图已生成并计数）", level="warn")
    if cancelled:
        raise asyncio.CancelledError()
    return result


async def upstream_call(url: str, payload: dict, accept: str = "*/*", *,
                        on_rate_limited=None, requires_anlas: bool = False,
                        v5_free: bool = False, image_count: int = 0,
                        image_lane: bool = False, resolve_v5_cost=None,
                        max_response_bytes: int | None = None) -> httpx.Response:
    state = _state()
    main = _main()
    try:
        return await state.nai.request(
            "POST", url, payload, accept=accept, on_rate_limited=on_rate_limited,
            requires_anlas=requires_anlas, v5_free=v5_free, image_count=image_count,
            image_lane=image_lane,
            **({"queue_timeout": state.settings.queue_timeout,
                "before_dispatch": main.check_image_cooldown} if image_lane else {}),
            **({"max_response_bytes": max_response_bytes} if max_response_bytes else {}),
            **({"resolve_v5_cost": resolve_v5_cost} if resolve_v5_cost is not None else {}),
        )
    except UpstreamError as e:
        raise main.GateError(e.status if e.status in (429, 503) else 502, e.message,
                             billing_uncertain=e.billing_uncertain) from None


async def wait_for_user_image_slot(key) -> None:
    if key["is_admin"]:
        return
    try:
        await _state().wait_for_key_image_slot(key["id"])
    except TimeoutError:
        raise _main().err(429, "图片任务排队超时，请稍后再试") from None


async def image_tool(request: Request, operation: str):
    main = _main()
    state = _state()
    key = await main.authenticate(request)
    main.check_image_cooldown()
    await main.check_rpm(key)
    await main.require_feature(key, "upscale" if operation == "upscale" else "augment")
    if not key["is_admin"] and not (key["allow_img2img"] and state.settings.allow_img2img):
        raise main.err(403, "此 Key 或本站未开放图片处理权限（img2img）")
    body = await read_image_payload(request)
    await wait_for_user_image_slot(key)
    async with acquire_concurrency(key, image=True):
        main.check_image_cooldown()
        try:
            payload, cost = await anyio.to_thread.run_sync(prepare_tool, body, operation)
        except ValueError as exc:
            raise main.err(400, str(exc)) from None
        model = payload.get("model", payload.get("req_type"))
        estimate = {"anlas": cost, "v5": 0}

        async def limited(retry_after):
            await state.block_image_generation(max(
                state.settings.image_429_cooldown_seconds, retry_after))

        async def perform():
            billing_uncertain = False
            try:
                resp = await upstream_call(
                    f"{state.nai.image_host}/ai/{operation}", payload,
                    on_rate_limited=limited, requires_anlas=cost > 0,
                    image_lane=True, max_response_bytes=MAX_RESPONSE_BYTES,
                )
                billing_uncertain = resp.status_code in (200, 201) or resp.status_code >= 500
                if resp.status_code not in (200, 201):
                    raise main.err(resp.status_code if 400 <= resp.status_code < 500 else 502,
                              f"图片工具请求失败（上游状态 {resp.status_code}），未记费")
                media, count = await anyio.to_thread.run_sync(
                    validate_result, resp.content, operation, payload.get("req_type", ""))
            except (main.GateError, httpx.HTTPError, ValueError, TimeoutError) as exc:
                billing_uncertain |= isinstance(exc, main.GateError) and exc.billing_uncertain
                await main.record(key, operation, model, "error",
                             detail=exc.message if isinstance(exc, main.GateError) else "图片工具连接失败或返回无效结果，未记费",
                             unconfirmed_anlas=cost if billing_uncertain else 0)
                if isinstance(exc, main.GateError):
                    raise
                raise main.err(502, "图片工具连接失败或返回无效结果，未记费；请勿自动重试") from None
            await main.settle_record(key, operation, model, "ok", images=count, anlas=cost,
                                detail=f"图片工具 {cost} Anlas；返回 {count} 张")
            return Response(resp.content, media_type=media, headers={"Cache-Control": "no-store"})

        async with reserve_image_budget(key, estimate):
            return await complete_image_operation(perform())


async def upscale_image(request: Request):
    return await image_tool(request, "upscale")


async def augment_image(request: Request):
    return await image_tool(request, "augment-image")


async def encode_vibe(request: Request):
    """Encode a V4/V4.5 reference; only a successful binary result costs 2 Anlas."""
    main = _main()
    state = _state()
    key = await main.authenticate(request)
    main.check_image_cooldown()
    await main.check_rpm(key)
    await main.require_feature(key, "vibe")
    body = await read_image_payload(request)
    # Accept Launcher's spelling as well as the existing Gate client field.
    if "information_extracted" in body:
        value = body.pop("information_extracted")
        if "informationExtracted" in body and body["informationExtracted"] != value:
            raise main.err(400, "两种 informationExtracted 字段的值不一致")
        body["informationExtracted"] = value
    problem = validate_vibe_encoding(body)
    if problem:
        raise main.err(400, problem)
    model = body["model"]
    payload = {"image": body["image"], "model": model,
               "information_extracted": body["informationExtracted"]}
    estimate = {"anlas": VIBE_ENCODING_ANLAS, "v5": 0, "is_vibe": True}
    await quota_image_check(key, estimate)

    async def record_encoding_429(retry_after: float) -> None:
        await state.block_image_generation(max(
            state.settings.image_429_cooldown_seconds, retry_after
        ))

    async def perform_encoding():
        try:
            resp = await upstream_call(
                f"{state.nai.image_host}/ai/encode-vibe", payload,
                accept="application/octet-stream", on_rate_limited=record_encoding_429,
                requires_anlas=True, image_lane=True, max_response_bytes=MAX_RESPONSE_BYTES,
            )
        except (main.GateError, httpx.HTTPError, TimeoutError) as exc:
            await main.record(key, "vibe_encode", model, "error",
                         detail=exc.message if isinstance(exc, main.GateError) else "上游编码请求失败，未记费",
                         unconfirmed_anlas=VIBE_ENCODING_ANLAS
                         if isinstance(exc, main.GateError) and exc.billing_uncertain else 0)
            if isinstance(exc, main.GateError):
                raise
            raise main.err(502, "Vibe 编码连接失败，未记费；请勿自动重试") from None
        if resp.status_code not in (200, 201):
            await main.record(key, "vibe_encode", model, "error", detail=f"upstream {resp.status_code}",
                         unconfirmed_anlas=VIBE_ENCODING_ANLAS if resp.status_code >= 500 else 0)
            raise main.err(resp.status_code if 400 <= resp.status_code < 500 else 502,
                      f"Vibe 编码失败（上游状态 {resp.status_code}），未记费")
        content_type = resp.headers.get("content-type", "application/octet-stream").lower()
        if not resp.content or "json" in content_type or content_type.startswith("text/"):
            await main.record(key, "vibe_encode", model, "error", detail="上游未返回有效二进制编码，未记费",
                         unconfirmed_anlas=VIBE_ENCODING_ANLAS)
            raise main.err(502, "Vibe 编码未返回有效数据，未记费")
        await main.settle_record(key, "vibe_encode", model, "ok", anlas=VIBE_ENCODING_ANLAS,
                            detail=f"Vibe 编码 {VIBE_ENCODING_ANLAS} Anlas")
        return Response(resp.content, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    await wait_for_user_image_slot(key)
    async with acquire_concurrency(key, image=True):
        main.check_image_cooldown()
        async with reserve_image_budget(key, estimate):
            return await complete_image_operation(perform_encoding())


async def generate_image(request: Request):
    return await _generate_image(request, streaming=False)


async def generate_image_stream(request: Request):
    return await _generate_image(request, streaming=True)


async def _generate_image(request: Request, *, streaming: bool):
    main = _main()
    state = _state()
    key = await main.authenticate(request)
    main.check_image_cooldown()
    await main.check_rpm(key)
    await main.require_feature(key, "image")
    body = await read_image_payload(request)
    request_timing.set_model(str(body.get("model") or ""))
    request_timing.refine_client_from_payload(body)
    if not isinstance(body.get("parameters"), dict):
        raise main.err(400, "缺少 parameters 对象")
    try:
        normalize_image_request(body)
    except ValueError as exc:
        main.record(key, "image", str(body.get("model", "?"))[:80], "rejected", detail=str(exc))
        raise main.err(400, f"图片参数无效：{exc}") from None
    model = body["model"]
    fix_name_digit_weights(body)               # bm94199:: → bm94199 ::（否则上游当成权重 94199 → NaN）
    sanitize_and_translate_prompts(body)       # WebUI 语法 (tag:1.2) 转译为 1.2::tag::，并清理孤立破损 ::
    problem = upstream_parameter_problem(body)
    if problem:
        main.record(key, "image", model, "rejected", detail=problem[:120])
        raise main.err(400, problem)
    if any(body["parameters"].get(name) for name in REFERENCE_FIELDS):
        await main.require_feature(key, "vibe")      # 参考图 / Vibe 经由 generate-image 传入时同样受功能开关约束
    model_tier = image_model_tier(model)
    if model_tier is None:
        main.record(key, "image", model, "rejected", detail="未列入本站图片模型白名单")
        raise main.err(400, "不支持或尚未开放的图片模型")
    share = getattr(state, "share", None)
    is_test_call = bool(
        main._row_flag(key, "is_test")
        or main._row_flag(key, "is_admin")
        or (request.client and getattr(request.client, "host", "").startswith(("172.30.", "127.0.0.1", "10.", "192.168.")))
    )
    if share is not None and not is_test_call:
        try:                            # 防分享：出图习惯指纹（只在内存里保留哈希，见 share_guard.py）
            busy = bool(state.guard.image_inflight.get(key["id"], 0) > 0)
            await share.observe_habit(key, body, request_timing.snapshot().get("src", ""),
                                      request.headers.get("user-agent", ""), busy=busy, **main._share_callbacks(request))
        except Exception as exc:
            _bug("share_guard", exc)
    if model_tier == "v5" and not key["is_admin"] and key["image_model_scope"] != "all":
        main.record(key, "image", model, "rejected", detail="模型权限：仅允许 V4.5 及更低")
        raise main.err(403, "你的 Key 目前只能用 V4.5 及更低模型（V5 是全站共享的有限额度，暂时只开放给早期成员）。"
                       "请在客户端把模型换成 NAI Diffusion V4.5 再生成")

    # 图生图功能权限与费用分开判断；免费规格也沿用 Anlas 权限要求。
    if body.get("image") or body.get("mask"):
        raise main.err(400, "生图请求的 image 和 mask 请放在 parameters 中")
    problem = validate_image_references(body)
    if problem:
        raise main.err(400, problem)
    p0 = body.get("parameters", {}) or {}
    if (p0.get("image") or p0.get("mask")) and not key["is_admin"]:
        if not (key["allow_img2img"] and state.settings.allow_img2img):
            main.record(key, "image", model, "rejected", detail="img2img 未开放")
            raise main.err(400, "本站不支持图生图（img2img）/ 局部重绘（会消耗 Anlas）。请在客户端里移除参考图（原图）后再生成")
        if not key["allow_anlas"]:
            main.record(key, "image", model, "rejected", detail="img2img 未开通 Anlas 权限")
            raise main.err(402, "该 Key 未开通 Anlas 权限，无法使用图生图 / 局部重绘")

    # 免费档钳制：只对没有 Anlas 权限的 Key 生效；有 Anlas（含自动分配）的 Key 可以用超规格参数，按 Anlas 扣
    if state.settings.safe_clamp and not key["is_admin"] and not key["allow_anlas"]:
        try:
            body, notes, problem = clamp_image_params(
                body,
                max_pixels=state.settings.max_pixels,
                max_steps=state.settings.max_steps,
                allow_img2img=True,  # 权限已在上面预检
            )
        except (TypeError, ValueError, OverflowError):
            raise main.err(400, "图片参数无效") from None
        if problem:
            main.record(key, "image", model, "rejected", detail=problem)
            raise main.err(400, problem)
    else:
        notes = []
    body, more = fill_v4_prompt(body)          # V4 / V4.5 缺结构化提示词时上游回 500：照官方网页端补上
    notes = list(notes) + more
    if model_tier == "v5":
        if is_v5_medium(str(body.get("model", ""))):
            body, more = medium_normalize(body)     # 成员自己选 Medium 时也按官方规则整理，免得上游拒绝
            notes = list(notes) + more
        model = body["model"]

    p = body.get("parameters", {})
    try:
        image_count = int(p.get("n_samples", 1) or 1)
    except (TypeError, ValueError):
        raise main.err(400, "n_samples 必须是正整数")
    if image_count < 1:
        raise main.err(400, "n_samples 必须是正整数")

    try:
        est = estimate_image_cost(body, is_opus=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise main.err(400, "图片参数无效，无法估算费用") from exc
    # V5 续杯：自动分配了 Anlas 的成员，当天个人 V5 用完后改用 Anlas 生成同规格 V5（受每日 Anlas 限额约束）
    # 注意：账号 V5 额度还有剩余时，NovelAI 对免费规格 V5 扣的是 V5 额度而不是 Anlas。所以「续杯」只在账号 V5
    # 额度真的用完（≤ 1%）时才走 Anlas，否则会悄悄多用全站共享的 V5 额度、影响其他人。
    v5_left = (json.loads(await state.db.get_setting("quota_algo_last", "{}") or "{}").get("v5") or {}).get("percent")
    if (est["v5"] and _anlas_auto(key) and key["allow_anlas"] and key["daily_v5"] > 0
            and v5_left is not None and v5_left <= 1):
        if (await state.db.get_counter(key["id"], state.day()))["v5"] >= key["daily_v5"]:
            est = {**estimate_image_cost(body, is_opus=True, v5_allowance_available=False), "topup": True}
            notes = list(notes) + ["今日 V5 已用完，使用自动分配的 Anlas 续杯"]
    legacy_free_images = (
        1 if model_tier == "legacy" and legacy_normal_free_eligible(body) else 0
    )
    if any(p.get(name) for name in REFERENCE_FIELDS):
        est["is_vibe"] = True
    if not est["v5"]:
        await quota_image_check(key, est, legacy_free_images=legacy_free_images)

    cost = "; ".join(part for part in (
        f"est={est['anlas']}A" if est["anlas"] else "",
        f"V5额度+{est['v5']}" if est["v5"] else "",
    ) if part) or "免费"
    # 尺寸 / 步数 / 费用始终在前：钳制说明只追加在后面，不能顶掉记账信息
    detail = "; ".join([f"{p.get('width')}x{p.get('height')}/{p.get('steps')}step {cost}", *notes])

    reservation = None

    async def resolve_v5_cost(exhausted: bool):
        nonlocal est, detail
        if exhausted and not key["allow_anlas"]:
            # 账号的 V5 免费额度刚用完：直说原因，不要报成「你的 Key 没开通付费权限」（审查 P2）
            raise main.err(402, "账号今天的 V5 免费额度刚用完（不是你的个人额度）。可以先改用 V4.5，额度恢复后再用 V5", reasons.V5_EXHAUSTED)
        updated = estimate_image_cost(body, is_opus=True, v5_allowance_available=not exhausted)
        await reservation.update(key, updated, legacy_free_images)
        est = updated
        if exhausted:
            detail = "; ".join(notes + [
                f"{p.get('width')}x{p.get('height')}/{p.get('steps')}step est={est['anlas']}A",
                "官方确认 V5 额度不可用，按 Anlas 估算记账"])

    async def record_image_429(retry_after: float) -> None:
        # 两种响应均在收尾时记日志，回调只更新冷却。
        await state.block_image_generation(max(
            state.settings.image_429_cooldown_seconds, retry_after
        ))

    async def perform_generation():
        try:
            resp = await upstream_call(
                f"{state.nai.image_host}/ai/generate-image", body,
                on_rate_limited=record_image_429,
                requires_anlas=est["anlas"] > 0,
                v5_free=est["v5"] > 0,
                image_count=image_count,
                # Retain the status to distinguish rejected requests from uncertain charges.
                image_lane=True, max_response_bytes=MAX_RESPONSE_BYTES,
                resolve_v5_cost=resolve_v5_cost if est["v5"] else None,
            )
        except main.GateError as exc:
            if not request_timing.was_sent() and not exc.billing_uncertain:
                # 还没发到上游就被本地拒绝：记「拒绝」+ 原因码，不算上游失败（审查 F6）
                await main.record(key, "image", model, "rejected", detail=f"{exc.status} {exc.message}"[:160], reason=exc.code)
                raise
            await main.record(key, "image", model, "error", detail=exc.message,
                         unconfirmed_anlas=est["anlas"] if exc.billing_uncertain else 0)
            await main.audit_generation(key, "image", model, "error", body)
            main.upstream_outcome(False)
            raise
        if resp.status_code not in (200, 201):
            await main.record(key, "image", model, "error", detail=f"upstream {resp.status_code}",
                         unconfirmed_anlas=est["anlas"] if resp.status_code >= 500 else 0)
            await main.audit_generation(key, "image", model, "error", body)
            main.upstream_outcome(resp.status_code < 500)
            if resp.status_code >= 500:
                main.notify_owner("upstream_5xx", f"NovelAI 图片接口返回 {resp.status_code}，上游可能故障。", 1800)
            raise main.err(resp.status_code, upstream_error_message(resp.status_code))
        await main.settle_record(key, "image", model, "ok", images=image_count, anlas=est["anlas"],
                            v5=est["v5"], legacy_free_images=legacy_free_images,
                            detail=detail)
        main.schedule_audit(key, "image", model, "ok", body, resp.content)
        main.upstream_outcome(True)
        return Response(resp.content, status_code=200,
                        media_type=resp.headers.get("content-type", "application/octet-stream"))

    if streaming:
        # Existing panels default to SSE; Launcher explicitly requests MessagePack.
        wire_format = p.get("stream", "sse")
        if wire_format not in ("sse", "msgpack"):
            raise main.err(400, "stream 仅支持 sse 或 msgpack")
        body.setdefault("parameters", {})["stream"] = wire_format
        dispatched = False
        delivered = False          # 全部最终图都已推给客户端（之后客户端断开不算「没拿到图」）

        def on_dispatch():
            nonlocal dispatched
            dispatched = True

        async def perform_stream(response):
            nonlocal delivered
            local_reject = False
            tracker = ImageEventTracker(image_count, wire_format)
            failure = None
            billing_uncertain = False
            try:
                # Bound total drain time even when an upstream sends endless
                # progress frames that would keep resetting its read timeout.
                async with asyncio.timeout(300):
                    async with state.nai.image_stream(
                        f"{state.nai.image_host}/ai/generate-image-stream", body,
                        requires_anlas=est["anlas"] > 0, v5_free=est["v5"] > 0,
                        on_rate_limited=record_image_429,
                        on_dispatch=on_dispatch,
                        queue_timeout=state.settings.queue_timeout,
                        before_dispatch=main.check_image_cooldown,
                        resolve_v5_cost=resolve_v5_cost if est["v5"] else None,
                    ) as handle:
                        billing_uncertain = True
                        try:
                            content_type = handle.response.headers.get("content-type", "")
                            media_type = content_type.split(";", 1)[0].strip().lower()
                            allowed = {STREAM_MEDIA_TYPES[wire_format]}
                            if wire_format == "msgpack":
                                # The endpoint's Swagger still advertises SSE;
                                # validate binary framing even under that header.
                                allowed.update(("application/msgpack", "application/octet-stream",
                                                "text/event-stream"))
                            if media_type not in allowed:
                                raise UpstreamError(502, "上游未返回有效的图片事件流")
                            await response.start()
                            async for chunk in handle.response.aiter_bytes():
                                # Never forward part of an error event before it
                                # has been identified, even across network chunks.
                                for frame in tracker.frames(chunk):
                                    handle.completed_images = tracker.completed_images
                                    await response.chunk(frame)
                                if tracker.failed:
                                    raise UpstreamError(502, "上游流式生成失败")
                                if tracker.completed_images == image_count:
                                    delivered = True
                                    break
                            tracker.finish()
                            if tracker.completed_images < image_count:
                                raise UpstreamError(502, "图片流提前结束，未收到全部最终图片")
                        finally:
                            handle.completed_images = tracker.completed_images
            except (UpstreamError, ImageStreamProtocolError, httpx.HTTPError, TimeoutError) as exc:
                billing_uncertain |= (isinstance(exc, UpstreamError) and exc.billing_uncertain
                                      or isinstance(exc, TimeoutError) and dispatched)
                status = exc.status if isinstance(exc, UpstreamError) else 502
                message = exc.message if isinstance(exc, UpstreamError) else (
                    str(exc) if isinstance(exc, ImageStreamProtocolError) else "图片流连接中断或超时")
                # 上游 error 事件的原话只进日志，不转发给客户端（可能含上游内部信息）
                failure = message + (f"（上游：{tracker.error_detail}）" if tracker.error_detail else "")
                await response.error(status, message)
            except main.GateError:
                # 还没发到上游就被本地拒绝（冷却 / V5 用完等）：由 run_stream 记一条「拒绝」，
                # 这里不能再记「上游出错」，也不能算进上游失败率（2026-10-10 审查 F6）
                local_reject = not dispatched
                raise
            finally:
                completed = tracker.completed_images
                if not dispatched and not completed and failure is None and not local_reject:
                    # 还在排队时客户端就断开了：没发到上游，不能记成「上游出错」，也不能拉低上游健康度
                    await main.record(key, "image_stream", model, "cancelled", detail="排队时客户端断开（未发到上游，未记费）")
                elif not (local_reject and not completed):      # 本地拒绝且没出图：交给 run_stream 记「拒绝」
                    settled_anlas = 0
                    if completed:
                        # 按完整结果重算首张减免，沿用派发前确认的 V5 额度状态。
                        completed_body = {**body, "parameters": {**p, "n_samples": completed}}
                        settled = estimate_image_cost(
                            completed_body, v5_allowance_available=bool(est["v5"]))
                        settled_anlas = settled["anlas"]
                        await main.settle_record(
                            key, "image_stream", model, "ok", images=completed,
                            anlas=settled["anlas"], v5=settled["v5"],
                            legacy_free_images=legacy_free_images,
                            detail=detail + (f"; 完成 {completed}/{image_count}" if completed < image_count else ""),
                        )
                    await main.audit_generation(key, "image_stream", model, "ok" if completed and not failure else "error", body,
                                           tracker.first_image if completed else None)
                    main.upstream_outcome(bool(completed) and not failure)
                    if failure or not completed:
                        # 未结算部分单独记为待核对费用。
                        await main.record(key, "image_stream", model, "error",
                                     detail=failure or "未收到最终图片，未记费",
                                     unconfirmed_anlas=max(0, est["anlas"]-settled_anlas)
                                     if billing_uncertain else 0)

        async def run_stream(response):
            try:
                await wait_for_user_image_slot(key)
                async with acquire_concurrency(key, image=True):
                    main.check_image_cooldown()
                    async with reserve_image_budget(key, est, legacy_free_images=legacy_free_images) as reserved:
                        nonlocal reservation
                        reservation = reserved
                        await complete_image_operation(perform_stream(response), can_cancel=lambda: not dispatched,
                                                       delivered=lambda: delivered)
            except main.GateError as exc:
                # 和非流式一样记「拒绝」：自动驾驶（节约模式 / Key 限流）、AIMD、每日微调都靠这些记录感知拥挤
                main.record(key, "image_stream", model, "rejected", detail=f"{exc.status} {exc.message}"[:160], reason=exc.code)
                await response.error(exc.status, exc.message)

        return ImageStreamResponse(run_stream, wire_format)

    await wait_for_user_image_slot(key)
    async with acquire_concurrency(key, image=True):
        # Recheck both cooldown and quota after any queue/budget wait.
        main.check_image_cooldown()
        async with reserve_image_budget(key, est, legacy_free_images=legacy_free_images) as reservation:
            return await complete_image_operation(perform_generation())


async def suggest_tags(request: Request):
    main = _main()
    state = _state()
    key = await main.authenticate(request)
    main.check_image_cooldown()
    await main.require_feature(key, "tags")
    admitted = False
    try:
        # Bound body reads, semaphore waiting and the optional upstream lookup.
        async with asyncio.timeout(state.settings.queue_timeout):
            admitted = await state.wait_for_tag_request(key["id"])
            if admitted is None:              # 已被同一 Key 更新的补全查询取代：回空结果，不记拒绝
                return JSONResponse({"tags": []})
            if not admitted:
                raise main.err(429, "补全查询排队人数过多，请稍后再试")
            return await _suggest_tags(request, key)
    except TimeoutError:
        main.record(key, "tags", "", "error", detail="补全查询排队或请求超时")
        raise main.err(429, "补全查询等待超时，请稍后再试") from None
    finally:
        if admitted:
            await state.finish_tag_request(key["id"])


async def _suggest_tags(request: Request, key):
    main = _main()
    state = _state()
    if request.method == "GET":
        body = {
            "prompt": request.query_params.get("prompt", ""),
            "model": request.query_params.get("model", ""),
        }
    else:
        body = await main.read_json(request, limit_mb=0.0625)

    async def record_tag_429(retry_after: float) -> None:
        cooldown = await state.block_image_generation(max(
            state.settings.image_429_cooldown_seconds, retry_after
        ))
        main.record(
            key, "tags", "", "error",
            detail=(f"上游限流(429)：全站图片生成暂停约 {cooldown} 秒，"
                    "保护上游 Token"),
        )

    async with acquire_concurrency(key):
        if await request.is_disconnected():
            main._REQUEST_LOGGED.set(True)
            request_timing.mark_logged()         # 客户端已经换了新的查询，不算拒绝
            raise main.err(499, "补全查询已取消")
        main.check_image_cooldown()
        query = urlencode({
            "prompt": str(body.get("prompt", "") or ""),
            "model": str(body.get("model", "") or ""),
        })
        try:
            resp = await state.nai.request(
                "GET", f"{state.nai.image_host}/ai/generate-image/suggest-tags?{query}",
                accept="application/json", on_rate_limited=record_tag_429,
                image_lane=True, wait_for_image_slot=False)
        except UpstreamError as exc:
            if exc.status != 429:  # 真实上游 429 已由 record_tag_429 记录。
                main.record(key, "tags", "", "error", detail=exc.message)
            raise main.err(exc.status if exc.status in (429, 503) else 502, exc.message)
    if resp.status_code != 200:
        main.record(key, "tags", "", "error", detail=f"上游标签接口返回 HTTP {resp.status_code}")
        raise main.err(resp.status_code, upstream_error_message(resp.status_code))
    main.record(key, "tags", "", "ok")
    return Response(resp.content, media_type="application/json")


def register_routes(app, limit_inflight) -> None:
    for path in ("/ai/generate-image", "/nai/ai/generate-image", "/"):
        app.post(path)(limit_inflight(generate_image))
    for path in ("/ai/encode-vibe", "/nai/ai/encode-vibe"):
        app.post(path)(limit_inflight(encode_vibe))
    for path in ("/ai/upscale", "/nai/ai/upscale"):
        app.post(path)(limit_inflight(upscale_image))
    for path in ("/ai/augment-image", "/nai/ai/augment-image"):
        app.post(path)(limit_inflight(augment_image))
    for path in ("/ai/generate-image/suggest-tags", "/nai/ai/generate-image/suggest-tags"):
        app.post(path)(suggest_tags)
        app.get(path)(suggest_tags)
    for path in ("/ai/generate-image-stream", "/nai/ai/generate-image-stream"):
        app.post(path)(limit_inflight(generate_image_stream))
