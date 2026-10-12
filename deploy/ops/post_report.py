"""把实验室日报（report.png + summary.md）发到 Discord 公告频道。只用标准库。

默认 --dry-run：只打印将要发送的内容和附件，不发任何请求。加 --send 才真正发送。
  python3 deploy/ops/post_report.py --dir data/lab/2026-10-10            # dry-run
  DISCORD_BOT_TOKEN=... ANNOUNCE_CHANNEL_ID=... python3 deploy/ops/post_report.py --dir data/lab/2026-10-10 --send
multipart/form-data：payload_json（content + allowed_mentions 为空，不会 @ 任何人）+ files[0]=report.png。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API = "https://discord.com/api/v10"
MAX_CONTENT = 2000


def build(dir_: str) -> tuple[dict, str, bytes]:
    with open(os.path.join(dir_, "summary.md"), encoding="utf-8") as f:
        content = f.read().strip()[:MAX_CONTENT]
    png = os.path.join(dir_, "report.png")
    with open(png, "rb") as f:
        data = f.read()
    payload = {"content": content, "allowed_mentions": {"parse": []},
               "attachments": [{"id": 0, "filename": "report.png", "description": "服务器日报（统计图）"}]}
    return payload, "report.png", data


def multipart(payload: dict, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----lab" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{filename}\"\r\n"
        f"Content-Type: image/png\r\n\r\n".encode() + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="data/lab/YYYY-MM-DD")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="只打印（默认）")
    g.add_argument("--send", action="store_true", help="真的发送")
    a = ap.parse_args(argv)
    payload, name, data = build(a.dir)
    body, ctype = multipart(payload, name, data)
    if not a.send:
        print("[dry-run] 不发送。将发往频道 ANNOUNCE_CHANNEL_ID =", os.environ.get("ANNOUNCE_CHANNEL_ID", "(未设置)"))
        print(f"[dry-run] 附件 {name} {len(data)} 字节；multipart 共 {len(body)} 字节；allowed_mentions = {payload['allowed_mentions']}")
        print(payload["content"])
        return 0
    token, channel = os.environ.get("DISCORD_BOT_TOKEN"), os.environ.get("ANNOUNCE_CHANNEL_ID")
    if not token or not channel:
        print("缺少 DISCORD_BOT_TOKEN 或 ANNOUNCE_CHANNEL_ID", file=sys.stderr)
        return 2
    req = urllib.request.Request(f"{API}/channels/{channel}/messages", data=body, method="POST",
                                 headers={"Authorization": "Bot " + token, "Content-Type": ctype,
                                          "User-Agent": "nai-gate-lab (https://github.com, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("posted", r.status, json.loads(r.read()).get("id"))
            return 0
    except urllib.error.HTTPError as e:
        print("failed", e.code, e.read()[:300].decode("utf-8", "replace"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
