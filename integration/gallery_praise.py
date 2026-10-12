"""跑图分享自动评论：奶妹看图，写一段热情的夸奖（Markdown + 表情）。

模型：美团 LongCat（Anthropic 兼容接口，支持看图）。服务器在香港，Anthropic 官方 API 不开放该地区。
只有服务器 .env 里配置了 PRAISE_API_KEY 才启用；没配置时只点赞不评论。
每天最多 GALLERY_AI_DAILY 条（默认 30），防止被刷帖花超。
出错时不发评论（只记日志）；露骨图片或模型拒绝时发「捂眼睛跑开」。
"""
from __future__ import annotations

import base64
import os
import time
from typing import Optional

import httpx

BASE_URL = os.getenv("PRAISE_BASE_URL", "https://api.longcat.chat/anthropic").rstrip("/")
MODEL = os.getenv("PRAISE_MODEL", "LongCat-2.5-Preview")
DAILY_LIMIT = int(os.getenv("GALLERY_AI_DAILY", "30") or 30)
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 8 * 1024 * 1024

SYSTEM = """你是「猫头鹰公益站」Discord 社区的看板娘「奶妹」：一只软乎乎、元气满满的小猫头鹰，最喜欢看大家跑的 AI 图。
你在「跑图分享」频道给成员的作品写评论。

人设与语气：
- 自称「奶妹」，称呼作者为「老师」；不要猜测作者的性别。
- 热情、真诚、可爱，像一个真心喜欢这张图的同好，偶尔用「呜哇」「好耶」「嘿嘿」这类语气词，但不要每句都用。
- 夸得具体：先认真看图，说出画面里真实存在的亮点——人物外貌、表情、动作、服装、光影、配色、构图、画风、氛围。
- 结合作者写的标题和文字，回应他们的心情或梗。
- 不懂的不要编：看不清的细节不写；除非作者说了，否则不要说角色出自哪部作品。

格式：
- Markdown：一个二级标题开头（带一个表情），正文用 2～4 个小段或列表，可以用粗体和引用块；穿插适量表情符号。
- 长度 250～450 字。
- 结尾一句鼓励继续创作，可以邀请老师分享提示词思路。
- 只输出评论正文，不要解释、不要输出思考过程。

安全：如果画面含有露骨的性内容（裸露的性器官、性行为等），不要写评论，只输出四个字符：[NSFW]"""

# 露骨图片：不夸，奶妹捂眼睛跑开（模型拒绝时也用这句）
SHY = "🙈💨 呜哇——奶妹捂住眼睛跑开啦！\n这张对奶妹来说太刺激了，不敢看不敢看～ (〃▽〃)ﾉ 老师继续加油哦！\n\n——🦉 奶妹"

_used: dict[str, int] = {}
last_reason = ""          # 最近一次没出评论的原因（机器人上报给后台）


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def used_today() -> int:
    return _used.get(_today(), 0)


def enabled() -> bool:
    return bool(os.getenv("PRAISE_API_KEY"))


def _media_type(data: bytes) -> Optional[str]:
    """按文件头判断真实格式（Discord 的扩展名和 content_type 不一定准，接口会校验）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def _images(client: httpx.AsyncClient, urls: list[str]) -> list[dict]:
    """接口下载不了 Discord 的图片链接，所以先下载再以 base64 上传。"""
    out = []
    for u in urls[:3]:
        try:
            r = await client.get(u, timeout=20)
        except httpx.HTTPError:
            continue
        mt = _media_type(r.content) if r.status_code == 200 and len(r.content) <= MAX_IMAGE_BYTES else None
        if mt:
            out.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                    "data": base64.b64encode(r.content).decode()}})
    return out


def _skip(reason: str, bug: bool = False) -> None:
    global last_reason
    last_reason = reason
    print(f"[{'bug' if bug else 'gallery'}] gallery: {reason}", flush=True)     # [bug] 会被监控抓到


async def write_praise(title: str, text: str, image_urls: list[str], *, model: Optional[str] = None,
                       daily: Optional[int] = None) -> Optional[str]:
    """返回评论正文；没有可用图片、超出每日上限或出错时返回 None（原因见 last_reason）。"""
    if not enabled():
        _skip("没有配置 PRAISE_API_KEY")
        return None
    if not image_urls:
        _skip("帖子里没有图片")
        return None
    day, limit = _today(), DAILY_LIMIT if daily is None else daily
    if _used.get(day, 0) >= limit:
        _skip(f"今天的 AI 评论已达上限 {limit} 条")
        return None
    headers = {"Authorization": "Bearer " + os.environ["PRAISE_API_KEY"], "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient() as client:
        content = await _images(client, image_urls)
        if not content:
            _skip("图片下载失败或格式不支持")
            return None
        content.append({"type": "text", "text": f"帖子标题：{title or '（无）'}\n作者的话：{text or '（无）'}\n\n请为这个帖子写评论。"})
        try:
            r = await client.post(f"{BASE_URL}/v1/messages", headers=headers, timeout=120, json={
                "model": model or MODEL, "max_tokens": 4000, "system": SYSTEM,
                "messages": [{"role": "user", "content": content}],
            })
        except httpx.HTTPError as exc:
            _skip(f"AI 评论网络错误 {type(exc).__name__}", bug=True)
            return None
    if r.status_code == 429:
        _skip("AI 评论接口限流")
        return None
    if r.status_code != 200:
        _skip(f"AI 评论失败：{r.status_code} {r.text[:200]}", bug=True)
        return None
    data = r.json()
    if data.get("stop_reason") == "refusal":
        print("[gallery] AI comment refused by model; shy reply", flush=True)
        return SHY
    # 模型会先输出 thinking 块，只取正文
    body = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not body:
        _skip("模型没有返回正文")
        return None
    if body.startswith("[NSFW]"):
        print("[gallery] explicit image; shy reply", flush=True)
        return SHY
    _used[day] = _used.get(day, 0) + 1
    u = data.get("usage") or {}
    print(f"[gallery] AI comment ok in={u.get('input_tokens')} out={u.get('output_tokens')} today={_used[day]}", flush=True)
    return body[:1900] + "\n\n——🦉 奶妹"
