"""Standalone Discord bot.

成员：/register /quota /resetkey /help /反馈。管理员：/open /limit /slots /ban /unban /revoke。
功能授权和生成记录开关只在网页后台操作。
"""
from __future__ import annotations

import asyncio
import math
import os
import time

import discord
import httpx
from discord import app_commands

from discord_registration import handle_register
import gallery_praise

BACKEND = os.getenv("REGISTRATION_BACKEND_URL", "http://127.0.0.1:3003").rstrip("/")
SITE = os.getenv("SITE_URL", "").rstrip("/")


# 后台「Discord」页的配置；每分钟从网关读一次（读不到时沿用上一次 / 默认值）
# 拉不到后台配置时一律按「关」处理（fail-closed）：以前默认 1，后台连不上就会自动点赞 / AI 评论
CONFIG = {"gallery_forum": os.getenv("GALLERY_FORUM_NAME", "跑图分享"), "gallery_like": 0, "gallery_ai": 0,
          "gallery_ai_daily": gallery_praise.DAILY_LIMIT, "gallery_ai_model": gallery_praise.MODEL}


async def bridge(method: str, path: str, payload: dict | None = None):
    """调网关的机器人桥接接口（/self-register/bot/*）；失败返回 None，不影响机器人其他功能。"""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.request(method, BACKEND + "/self-register/bot" + path, json=payload,
                                     headers={"Authorization": "Bearer " + os.environ["REGISTRATION_BRIDGE_SECRET"]})
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def report_event(kind: str, thread: discord.Thread, author: str = "", detail: str = "") -> None:
    await bridge("POST", "/report", {"event": {"kind": kind, "title": thread.name, "author": author, "detail": detail,
                                               "url": thread.jump_url}})


async def sync_loop(client: discord.Client) -> None:
    """每分钟：拉配置、报心跳（后台据此显示在线状态）。"""
    ready_at = time.time()
    while not client.is_closed():
        try:
            cfg = await bridge("GET", "/config")
            if isinstance(cfg, dict):
                CONFIG.update({k: v for k, v in cfg.items() if k in CONFIG})
            g = client.get_guild(int(os.environ["DISCORD_GUILD_ID"]))
            guild = g.name if g else ""
            latency = 0
            try:
                lat = float(getattr(client, "latency", 0.0) or 0.0)
                if math.isfinite(lat):
                    latency = round(lat * 1000)
            except Exception:
                latency = 0
            await bridge("POST", "/report", {"status": {
                "user": str(client.user), "guild": guild, "latency_ms": latency,
                "ready_at": ready_at, "ai_ready": gallery_praise.enabled(), "ai_today": gallery_praise.used_today()}})
        except Exception as exc:
            print(f"[sync_loop] heart-beat warning: {exc}", flush=True)
        await asyncio.sleep(60)


STATUS_TEXT = {"ok": "🟢 正常", "idle": "🟢 正常（近期无请求）", "degraded": "🟠 不稳定（近期失败较多）"}


async def backend(path: str, interaction: discord.Interaction, extra_id: str | None = None,
                  extra: dict | None = None):
    """POST to the gateway bridge. Returns (status, json-or-detail-text)."""
    payload = {"discord_id": extra_id or str(interaction.user.id), "guild_id": str(interaction.guild_id),
               "actor_id": str(interaction.user.id), **(extra or {})}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(BACKEND + path, json=payload,
                                         headers={"Authorization": "Bearer " + os.environ["REGISTRATION_BRIDGE_SECRET"]})
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return 0, "服务暂时不可用，请稍后再试。"
    return response.status_code, data if response.status_code == 200 else data.get("detail", "请求失败。")


def build_client() -> tuple[discord.Client, app_commands.CommandTree, discord.Object]:
    guild = discord.Object(id=int(os.environ["DISCORD_GUILD_ID"]))
    intents = discord.Intents.none()
    intents.guilds = True            # 收到「论坛新帖」事件（非特权）；斜杠命令也只需要它
    intents.guild_messages = True    # 收到频道消息事件（非特权），只用来识别「@奶妹」；不读正文，被 @ 的消息自带 mentions
    # Message Content 是特权 Intent，原本用于画廊 AI 评论读帖子正文。画廊自动互动已关闭，
    # 不再需要它；为降低特权足迹（配合 Discord 申诉）这里不再申请。若将来恢复画廊 AI 评论，
    # 需在此加回 intents.message_content=True，并在开发者后台重新开启该 Intent。
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    @tree.command(name="register", description="领取你的猫头鹰公益站 API Key", guild=guild)
    @app_commands.describe(key="收到站长发的邀请 Key 时粘贴在这里，绑定到你的账号（可选）")
    async def register(interaction: discord.Interaction, key: str | None = None):
        await handle_register(interaction, (key or "").strip())

    @tree.command(name="quota", description="查看今日额度、Key 有效期和服务状态", guild=guild)
    async def quota(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        status, data = await backend("/self-register/quota", interaction)
        if status != 200:
            await interaction.followup.send(str(data), ephemeral=True)
            return
        lines = [f"今日图片：{data['images']} / {data['daily_images']}"]
        if data.get("daily_images_base"):
            lines.append(f"（保底 {data['daily_images_base']} 张；超过后在全站空闲时可继续用到 {data['daily_images']} 张）")
        if data["daily_v5"]:
            lines.append(f"今日 V5：{data['v5']} / {data['daily_v5']}")
        scope = "含 V5" if data["image_model_scope"] == "all" else "仅 V4.5 及以下"
        lines.append(f"可用模型：{scope}")
        lines.append("已开通功能：" + ("、".join(f["label"] for f in data["features"] if f["on"]) or "无"))
        closed = [f["label"] for f in data["features"] if not f["on"]]
        if closed:
            lines.append("未开通：" + "、".join(closed))
        if data["expires_at"]:
            days = max(0, int((data["expires_at"] - time.time()) // 86400))
            lines.append(f"Key 剩余有效期：约 {days} 天")
        if not data["enabled"]:
            lines.append("⚠ 这把 Key 已被停用，请联系站长。")
        code, info = await backend("/self-register/info", interaction)
        if code == 200:
            up = info["upstream"]
            state = f"服务状态：{STATUS_TEXT.get(up['status'], up['status'])}"
            if up["recent"]:
                state += f"（近 10 分钟 {up['recent']} 次请求，失败 {up['failed']} 次）"
            lines.append(state)
            if up["image_cooldown_seconds"]:
                lines.append(f"⏳ 上游限流冷却中，约 {up['image_cooldown_seconds']} 秒后恢复生图")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @tree.command(name="反馈", description="给站长提意见：5 道选择题，30 秒填完（只有你能看到，不会私信你）", guild=guild)
    async def feedback_cmd(interaction: discord.Interaction):
        """弹出 Discord 自带表单。成员自己发起，不私信、不群发（680009 申诉期规则）。"""
        status, data = await backend("/self-register/feedback/questions", interaction)
        if status != 200 or not data.get("questions"):
            await interaction.response.send_message("问卷暂时打不开，请稍后再试。", ephemeral=True)
            return
        qs = data["questions"][:5]

        class FeedbackModal(discord.ui.Modal, title="猫头鹰公益站 · 反馈问卷"):
            pass

        modal = FeedbackModal(timeout=900)
        inputs = []
        for q in qs:      # 每题一个必填下拉单选（discord.py 2.6+ 的 Label 包 Select）
            field = discord.ui.Select(placeholder="请选择", required=True, min_values=1, max_values=1,
                                      options=[discord.SelectOption(label=str(o)[:100]) for o in q["options"][:25]])
            modal.add_item(discord.ui.Label(text=str(q["label"])[:45], component=field))
            inputs.append((q["id"], field))

        async def on_submit(modal_interaction: discord.Interaction):
            await modal_interaction.response.defer(ephemeral=True)
            answers = {qid: (field.values[0] if field.values else "") for qid, field in inputs}
            code, result = await backend("/self-register/feedback", modal_interaction,
                                         extra={"answers": answers,
                                                "username": str(getattr(modal_interaction.user, "name", "") or "")[:80]})
            text = result.get("message", "收到啦～") if code == 200 and isinstance(result, dict) else str(result)
            await modal_interaction.followup.send(text, ephemeral=True)

        modal.on_submit = on_submit
        await interaction.response.send_modal(modal)

    @tree.command(name="resetkey", description="Key 丢了或泄露了？换一把新的（旧的立即失效）", guild=guild)
    async def resetkey(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        status, data = await backend("/self-register/resetkey", interaction)
        if status != 200:
            await interaction.followup.send(str(data), ephemeral=True)
            return
        await interaction.followup.send(f"新的 Key（只有你能看到这条消息，请妥善保存）：\n`{data['key']}`\n旧 Key 已失效。",
                                        ephemeral=True)

    @tree.command(name="help", description="怎么使用猫头鹰公益站", guild=guild)
    async def help_(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        status, data = await backend("/self-register/info", interaction)
        lines = [f"`/register` 领取 Key（私信发送，含配置教程）· `/quota` 额度和服务状态 · `/resetkey` 换新 Key · `/反馈` 给站长提意见",
                 f"客户端接口地址填 `{SITE}`（不加 /v1），Key 填 `nai-` 开头的整串。详细说明：{SITE}"]
        if status == 200:
            if not data["open"]:
                lines.append("⚠ 目前暂未开放领 Key。")
            if data["notice"]:
                lines.append("📢 " + data["notice"])
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    async def run_ops(interaction: discord.Interaction, action: str, **extra):
        await interaction.response.defer(ephemeral=True)
        code, data = await backend("/self-register/ops", interaction, extra={"action": action, **extra})
        await interaction.followup.send(data["message"] if code == 200 else str(data), ephemeral=True)

    @tree.command(name="open", description="（管理员）开放或关闭领 Key", guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(state=[app_commands.Choice(name="开放", value="on"), app_commands.Choice(name="关闭", value="off")])
    async def open_(interaction: discord.Interaction, state: app_commands.Choice[str]):
        await run_ops(interaction, "open", value=state.value)

    @tree.command(name="limit", description="（管理员）设置名额上限，0 为不限", guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    async def limit(interaction: discord.Interaction, count: app_commands.Range[int, 0, 1000]):
        await run_ops(interaction, "limit", value=str(count))

    @tree.command(name="ban", description="（管理员）永久禁止某位成员领取 Key，并撤销其现有 Key", guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    async def ban(interaction: discord.Interaction, member: discord.Member):
        await run_ops(interaction, "ban", target=str(member.id))

    @tree.command(name="unban", description="（管理员）解除某位成员的领取禁令", guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    async def unban(interaction: discord.Interaction, member: discord.Member):
        await run_ops(interaction, "unban", target=str(member.id))

    @tree.command(name="slots", description="（管理员）查看名额占用", guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    async def slots(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        status, data = await backend("/self-register/slots", interaction)
        if status != 200:
            await interaction.followup.send(str(data), ephemeral=True)
            return
        cap = data["max"] or "不限"
        reset = f"，每天 {data['reset_at']} 自动清空" if data["reset_at"] else ""
        state = "开放中" if data.get("open", True) else "已关闭"
        wait = f"；候补 {data.get('waitlist', 0)} 人（已邀请 {data.get('invited', 0)} 人）" if data.get("waitlist") else ""
        await interaction.followup.send(f"已领取 {data['active']} / {cap}{reset}；领 Key {state}{wait}", ephemeral=True)

    @tree.command(name="revoke", description="（管理员）撤销某位成员的 Key，释放名额", guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    async def revoke(interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        status, data = await backend("/self-register/revoke", interaction, extra_id=str(member.id))
        await interaction.followup.send(f"已撤销 {member.mention} 的 Key。" if status == 200 else str(data),
                                        ephemeral=True)

    @client.event
    async def on_thread_create(thread: discord.Thread):
        """「跑图分享」论坛有新帖：奶妹自动点赞，再看图写一段评论（开关和频道名在后台「Discord」页）。"""
        parent = thread.parent
        if parent is None or CONFIG["gallery_forum"] not in (parent.name or ""):
            return
        print(f"[gallery] new post thread={thread.id} owner={thread.owner_id} title={thread.name[:40]}", flush=True)
        starter = None
        for attempt in range(3):            # 论坛帖的首条消息可能比建帖事件晚一点到
            try:
                starter = thread.starter_message or await thread.fetch_message(thread.id)
                break
            except discord.NotFound:
                await asyncio.sleep(2)
            except discord.HTTPException as exc:
                print(f"[bug] gallery fetch failed: {exc}", flush=True)
                await report_event("error", thread, detail=f"读取帖子失败：{exc}")
                return
        if starter is None:
            return
        author = starter.author.display_name if starter.author else ""
        if CONFIG["gallery_like"]:
            try:
                await starter.add_reaction("❤️")
                await report_event("like", thread, author)
            except discord.HTTPException as exc:
                print(f"[bug] gallery reaction failed: {exc}", flush=True)
                await report_event("error", thread, author, f"点赞失败：{exc}")
        if not CONFIG["gallery_ai"]:
            return
        images = [a.url for a in starter.attachments if (a.content_type or "").split(";")[0] in gallery_praise.IMAGE_TYPES]
        if not images:
            await report_event("skip", thread, author, "帖子里没有图片")
            return
        text = await gallery_praise.write_praise(thread.name, starter.content, images,
                                                 model=CONFIG["gallery_ai_model"], daily=CONFIG["gallery_ai_daily"])
        if not text:
            await report_event("skip", thread, author, gallery_praise.last_reason or "没有生成评论")
            return
        try:
            await thread.send(text, allowed_mentions=discord.AllowedMentions.none())
            await report_event("shy" if text == gallery_praise.SHY else "comment", thread, author, text[:100])
        except discord.HTTPException as exc:
            print(f"[bug] gallery comment send failed: {exc}", flush=True)
            await report_event("error", thread, author, f"发评论失败：{exc}")

    mention_last: dict[int, float] = {}

    @client.event
    async def on_message(message: discord.Message):
        """有人在频道里 @奶妹 → 回一句，并 @ 服务器主人（站长 10/10 要求）。
        同一频道 60 秒只回一次，防刷屏；@everyone / @身份组 不算，机器人消息不理。"""
        if (message.author.bot or message.guild is None or client.user is None
                or not any(f"<@{m}{client.user.id}>" in (message.content or "") for m in ("", "!"))):
            return  # 只认正文里明确的 @奶妹；回复奶妹的消息（自动带 ping）不算
        now = time.monotonic()
        # 同一频道 60 秒、同一成员 5 分钟、全服 20 秒各一次，防多频道刷屏（申诉期少发公开消息）
        if (now - mention_last.get(message.channel.id, -1e9) < 60
                or now - mention_last.get(-message.author.id, -1e9) < 300
                or now - mention_last.get(0, -1e9) < 20):
            return
        if len(mention_last) > 500:
            mention_last.clear()
        mention_last[message.channel.id] = mention_last[-message.author.id] = mention_last[0] = now
        owner = message.guild.owner_id
        try:
            await message.reply(f"我在床上陪主人呢 <@{owner}>",
                                allowed_mentions=discord.AllowedMentions(users=[discord.Object(owner)], everyone=False,
                                                                         roles=False, replied_user=False))
            print(f"[mention] replied in #{getattr(message.channel, 'name', message.channel.id)} to {message.author.id}", flush=True)
        except discord.HTTPException as exc:
            print(f"[bug] mention reply failed: {exc}", flush=True)

    @tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        """命令出错：打印完整堆栈（监控会抓到），并告诉成员不是他的问题。"""
        import traceback
        name = interaction.command.name if interaction.command else "?"
        print(f"[bug] bot command /{name} failed: {type(error).__name__}: {error}", flush=True)
        traceback.print_exception(type(error), error, error.__traceback__)
        text = "出了点问题，已自动记录，请稍后再试；一直不行请在频道里告诉站长。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    @client.event
    async def on_ready():
        await tree.sync(guild=guild)
        print(f"[bot] ready as {client.user}, commands synced", flush=True)
        if not getattr(client, "_sync_started", False):     # 断线重连也会触发 on_ready，只启动一次
            client._sync_started = True
            asyncio.create_task(sync_loop(client))

    return client, tree, guild


def main() -> None:
    client, _tree, _guild = build_client()
    client.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
