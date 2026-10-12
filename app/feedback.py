"""成员反馈：/反馈 命令弹出 Discord 表单（5 道必填单选题），答案存库，后台查看与导出。

不私信、不群发：成员自己发起，回执只有本人可见（680009 申诉期规则）。
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime

# 全部是必填单选题（站长 10/11：不要填空题）。Discord 弹窗最多 5 题，题目 ≤45 字，每题最多 25 个选项、选项 ≤100 字。
# 改这里，下次有人打开 /反馈 就生效（机器人每次现取题目）。
QUESTIONS = [
    {"id": "score", "label": "整体用下来感觉怎么样？",
     "options": ["非常满意", "满意", "一般", "不太满意", "很不满意"]},
    {"id": "pain", "label": "目前最困扰你的是？",
     "options": ["排队太久 / 经常提示人多", "每天张数不够用", "V5 额度太少", "出图失败或报错", "不会配置 / 教程看不懂", "没什么困扰"]},
    {"id": "wish", "label": "最希望优先改进哪一项？",
     "options": ["多给一些 V5", "多给一些 V4.5 张数", "出图更快、少排队", "开放更多功能（放大、导演工具等）", "更清楚的教程", "保持现状就好"]},
    {"id": "client", "label": "你主要用什么来出图？",
     "options": ["网页前端（类似官网）", "SillyTavern 等聊天前端", "ComfyUI / SD WebUI 类工具", "手机 App", "其他"]},
    {"id": "freq", "label": "你多久用一次公益站？",
     "options": ["每天都用", "每周几次", "偶尔用", "刚来还没怎么用"]},
]
COOLDOWN = 600          # 同一个人 10 分钟内只收一份，防刷


def clean(answers: dict) -> dict:
    """只收题目里有的选项；不认识的题目和选项丢掉。"""
    out = {}
    for q in QUESTIONS:
        v = str((answers or {}).get(q["id"], "") or "").strip()
        if v in q["options"]:
            out[q["id"]] = v
    return out


async def submit(db, discord_id: str, username: str, key_id, answers: dict) -> str:
    """存一份反馈。返回给本人看的回执文字；不合格抛 ValueError。"""
    a = clean(answers)
    if len(a) < len(QUESTIONS):
        raise ValueError("有题目没选，请重新打开 /反馈 选完再提交。")
    last = await db._db.execute_fetchall("SELECT MAX(ts) FROM feedback WHERE discord_id=?", (discord_id,))
    if last and last[0][0] and time.time() - last[0][0] < COOLDOWN:
        raise ValueError("刚刚已经收到你的反馈啦，过 10 分钟再提交新的吧。")
    await db._db.execute("INSERT INTO feedback (ts, discord_id, username, key_id, answers) VALUES (?,?,?,?,?)",
                         (time.time(), discord_id, username[:80], key_id, json.dumps(a, ensure_ascii=False)))
    await db._db.commit()
    return "收到啦，谢谢你的反馈～奶妹会转给站长 ✨"


async def listing(db, limit: int = 200) -> list[dict]:
    rows = await db._db.execute_fetchall(
        "SELECT f.id, f.ts, f.discord_id, f.username, f.key_id, f.answers, k.name FROM feedback f "
        "LEFT JOIN api_keys k ON k.id=f.key_id ORDER BY f.ts DESC LIMIT ?", (limit,))
    return [{"id": r[0], "ts": r[1], "discord_id": r[2], "username": r[3], "key_id": r[4],
             "answers": json.loads(r[5] or "{}"), "key_name": r[6] or ""} for r in rows]


def to_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "discord_id", "username", "key_id", "key_name"] + [q["label"] for q in QUESTIONS])
    from .client_export import safe_cell
    for it in sorted(items, key=lambda x: x["ts"]):
        w.writerow([safe_cell(c) for c in [datetime.fromtimestamp(it["ts"]).strftime("%Y-%m-%d %H:%M:%S"), it["discord_id"], it["username"],
                    it["key_id"] or "", it["key_name"]] + [it["answers"].get(q["id"], "") for q in QUESTIONS]])
    return ("﻿" + buf.getvalue()).encode("utf-8")
