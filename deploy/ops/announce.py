import os, sys, httpx
text = sys.stdin.read().strip()
r = httpx.post(f"https://discord.com/api/v10/channels/{os.environ['ANNOUNCE_CHANNEL_ID']}/messages",
               headers={"Authorization": "Bot " + os.environ["DISCORD_BOT_TOKEN"]},
               json={"content": text[:1900], "allowed_mentions": {"parse": []}}, timeout=10)
print("posted", r.status_code, r.json().get("id") if r.status_code == 200 else r.text[:200])
