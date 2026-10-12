"""每天把「重点议员的交易 + 任何议员的大额交易」推到 Discord 的 money 频道（Webhook，不经过奶妹）。

数据：Bargo.ai 免费接口（House + Senate STOCK Act 申报；不用 Key 每天 30 次 / 100 行，这里每天只用 1 次）。
条款：显示来源链接、不转发原始数据。申报最多晚 45 天；只是信息，不是投资建议。

服务器上：cron 每天 9:05 运行  python3 /opt/backups/congress_push.py
Webhook 地址放在 /opt/service-tools/.env 的 CONGRESS_WEBHOOK_URL（不进仓库）。推过的记在 STATE，不重复推。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = "https://www.bargo.ai/free-apis/congress/v1"
ENV = "/opt/service-tools/.env"
STATE = os.environ.get("CONGRESS_STATE", "/opt/backups/congress_state.json")
# 公认最受关注的议员（2024–2025 收益 / 关注度榜单常客）；名字按申报里的写法匹配
WATCH = ["Pelosi", "Khanna", "Greene", "Tim Moore", "Cruz", "Davidson", "Norcross", "Sewell", "Padilla", "Rick Scott"]
BIG = 250_001               # 金额区间下限 ≥ 25 万美元算大额
TYPE = {"purchase": "🟢 买入", "buy": "🟢 买入", "sale": "🔴 卖出", "sell": "🔴 卖出", "sale_full": "🔴 全部卖出",
        "sale_partial": "🔴 部分卖出", "exchange": "🔁 置换"}
CHAMBER = {"house": "众议员", "senate": "参议员"}


def webhook_url() -> str:
    url = os.environ.get("CONGRESS_WEBHOOK_URL", "")
    if not url and os.path.exists(ENV):
        for ln in open(ENV, encoding="utf-8"):
            if ln.startswith("CONGRESS_WEBHOOK_URL="):
                url = ln.split("=", 1)[1].strip()
    return url


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "owl-gate-congress/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def low_amount(rng: str) -> int:
    digits = "".join(c for c in (rng or "").split("-")[0] if c.isdigit())
    return int(digits) if digits else 0


def tid(t: dict) -> str:
    return "|".join(str(t.get(k, "")) for k in ("member_slug", "ticker", "transaction_date", "type", "amount_range", "disclosure_date"))


def line(t: dict) -> str:
    typ = TYPE.get(str(t.get("type", "")).lower().replace(" ", "_").replace("(", "").replace(")", ""), t.get("type") or "?")
    who = f"{t.get('member', '?')}（{CHAMBER.get(str(t.get('chamber', '')).lower(), '')} · {t.get('state') or ''}）"
    perf = t.get("perf_pct")
    perf_s = f" · 交易后 {perf:+.1f}%" if isinstance(perf, (int, float)) else ""
    return (f"{typ} **{t.get('ticker') or '—'}** {(t.get('asset') or '')[:30]}\n"
            f"　{who} · {t.get('amount_range') or '?'} · 交易 {t.get('transaction_date', '?')} · 申报 {t.get('disclosure_date', '?')}{perf_s}")


def post(url: str, content: str) -> None:
    body = json.dumps({"username": "国会交易速报", "content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "owl-gate-congress/1.0"})
    urllib.request.urlopen(req, timeout=20).read()
    time.sleep(1.5)


def main(test: bool = False) -> int:
    url = webhook_url()
    if not url:
        print("没有配置 CONGRESS_WEBHOOK_URL", file=sys.stderr)
        return 1
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"seen": []}
    seen = set(state["seen"])
    trades = get("/trades?limit=100").get("trades") or []
    pick = [t for t in trades if tid(t) not in seen and (any(w.lower() in str(t.get("member", "")).lower() for w in WATCH)
                                                          or low_amount(t.get("amount_range")) >= BIG)]
    if test:
        pick = pick[:5]
    if pick:
        head = "🏛️ **国会议员交易速报**" + ("（测试，只发 5 笔）" if test else f" · 新申报 {len(pick)} 笔") + \
               "\n重点议员：" + "、".join(WATCH[:6]) + " 等 · 以及任何议员 25 万美元以上的大额交易\n"
        chunks, cur = [], head
        for t in pick:
            ln = line(t) + "\n"
            if len(cur) + len(ln) > 1800:
                chunks.append(cur); cur = ""
            cur += ln
        cur += "\n-# 数据：<https://www.bargo.ai/free-apis/congress>（STOCK Act 申报，最多晚 45 天）· 仅供参考，不是投资建议"
        chunks.append(cur)
        for c in chunks:
            post(url, c)
    if not test:
        seen |= {tid(t) for t in trades}          # 这次看到的都记下，下次只推新的
        state["seen"] = list(seen)[-3000:]
        json.dump(state, open(STATE, "w"))
    print(f"新 {len(pick)} 笔，已推送" if pick else "没有新的重点交易")
    return 0


if __name__ == "__main__":
    sys.exit(main(test="--test" in sys.argv))
