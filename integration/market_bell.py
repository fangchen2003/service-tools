"""美股开盘 / 收盘提醒 → Discord money 频道（Webhook，不经过奶妹）。

看纳指 100、标普 500、道指有没有「穿越」MA250（最近 250 个交易日收盘均价）——看变化，不看高低：
前一天收盘在均线下、这天收盘在均线上 = 上穿；反过来 = 下穿。
同一天 ≥ 2 个指数上穿或下穿 → 🔴 红灯（几个指数互相印证，信号更可靠）；没有就是 🟢 绿灯（站长定的规则）。
开盘提醒看前一交易日收盘有没有穿越，收盘提醒看当天收盘。

数据：Yahoo Finance 公开图表接口（免费、不用 Key，每次 3 个请求）。只是信息，不是投资建议。
cron 每 5 分钟跑一次；只在纽约时间 9:30–9:45（开盘）和 16:05–16:30（收盘）真正取数，其余时间直接退出。
周末 / 美股假日：交易时段不是今天就不发。夏令时由纽约时区自动处理。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ENV = "/opt/service-tools/.env"
STATE = os.environ.get("MARKET_STATE", "/opt/backups/market_bell_state.json")
INDEXES = [("^NDX", "纳斯达克 100", "NDX"), ("^GSPC", "标普 500", "SPX"), ("^DJI", "道琼斯", "DJI")]
RED, GREEN = 0xE5484D, 0x30A46C
WEEK = "一二三四五六日"


def webhook_url() -> str:
    url = os.environ.get("MONEY_WEBHOOK_URL") or os.environ.get("CONGRESS_WEBHOOK_URL", "")
    if not url and os.path.exists(ENV):
        for ln in open(ENV, encoding="utf-8"):
            for k in ("MONEY_WEBHOOK_URL=", "CONGRESS_WEBHOOK_URL="):
                if ln.startswith(k) and not url:
                    url = ln.split("=", 1)[1].strip()
    return url


def chart(sym: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=2y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (owl-gate market bell)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())["chart"]["result"][0]
    rows = [(datetime.fromtimestamp(t, ET).date(), c) for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]) if c]
    return {"meta": d["meta"], "rows": rows}


def analyse(sym: str, name: str, short: str, mode: str, today) -> dict:
    c = chart(sym)
    rows = c["rows"]
    if mode == "close":
        if rows[-1][0] != today:
            raise LookupError("今天没有收盘数据（休市）")
        closes = [x[1] for x in rows]
    else:                                  # 开盘：只用已经收盘的日子
        closes = [x[1] for x in rows if x[0] < today]
    price = closes[-1] if mode == "close" else (c["meta"].get("regularMarketPrice") or closes[-1])
    prev = closes[-2] if mode == "close" else closes[-1]
    ma = sum(closes[-250:]) / 250
    ma_prev = sum(closes[-251:-1]) / 250
    above = (closes[-1] if mode == "close" else closes[-1]) > ma
    was_above = closes[-2] > ma_prev
    cross = "" if above == was_above else ("上穿" if above else "下穿")
    return {"name": name, "short": short, "sym": sym, "price": price, "chg": (price / prev - 1) * 100,
            "ma": ma, "dist": (closes[-1] / ma - 1) * 100, "above": above, "cross": cross}


def embed(mode: str, today, items: list[dict], test: bool) -> dict:
    crossed = [i for i in items if i["cross"]]
    red = len(crossed) >= 2
    light = "🔴 红灯" if red else "🟢 绿灯"
    title = ("🔔 美股开盘" if mode == "open" else "🌙 美股收盘") + f" · {today:%m 月 %d 日}（周{WEEK[today.weekday()]}）" + ("（测试）" if test else "")
    basis = "前一交易日收盘" if mode == "open" else "今日收盘"
    if crossed:
        what = "、".join(f"{i['name']}{i['cross']}" for i in crossed)
        summary = f"{len(crossed)} / 3 个指数{basis}穿越 MA250：{what}" + ("（互相印证）" if red else "（只有 1 个，还不算信号）")
    else:
        summary = f"{basis}没有指数穿越 MA250"
    fields = []
    for i in items:
        arrow = "▲" if i["chg"] >= 0 else "▼"
        pos = "在 MA250 之上" if i["above"] else "在 MA250 之下"
        lines = [f"**{i['price']:,.0f}**　{arrow} {i['chg']:+.2f}%" + ("（较昨收）" if mode == "open" else ""),
                 f"MA250　{i['ma']:,.0f}",
                 f"{'✅' if i['above'] else '⬇️'} {pos}　{i['dist']:+.1f}%"]
        if i["cross"]:
            lines.append(f"⚡ **{'今天' if mode == 'close' else '昨天'}{i['cross']} MA250**")
        q = urllib.parse.quote(i["sym"])
        lines.append(f"[行情](https://finance.yahoo.com/quote/{q}) · [图表](https://www.tradingview.com/chart/?symbol={i['short']})")
        fields.append({"name": i["name"], "value": "\n".join(lines), "inline": True})
    return {
        "title": title,
        "description": f"## {light}\n{summary}\n-# 同一天 ≥ 2 个指数上穿或下穿 MA250 为红灯（互相印证），没有就是绿灯",
        "color": RED if red else GREEN,
        "fields": fields,
        "footer": {"text": "MA250 = 最近 250 个交易日收盘均价 · 数据：Yahoo Finance · 仅供参考，不是投资建议"},
        "timestamp": datetime.now(ET).isoformat(),
    }


def post(url: str, payload: dict) -> None:
    body = json.dumps({"username": "美股开收盘", "embeds": [payload], "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "owl-gate-market/1.0"})
    urllib.request.urlopen(req, timeout=20).read()


def main(argv: list[str]) -> int:
    test = "--test" in argv
    now = datetime.now(ET)
    today = now.date()
    hm = now.hour * 60 + now.minute
    mode = "open" if 570 <= hm < 585 else "close" if 965 <= hm < 990 else None
    if test:
        mode = "open" if "open" in argv else "close"
    if mode is None:
        return 0
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if not test and state.get(mode) == str(today):
        return 0
    if not test and now.weekday() >= 5:
        return 0
    if mode == "open" and not test:        # 美股假日：今天的交易时段不是今天就不发
        reg = chart("^GSPC")["meta"]["currentTradingPeriod"]["regular"]
        if datetime.fromtimestamp(reg["start"], ET).date() != today:
            state[mode] = str(today); json.dump(state, open(STATE, "w"))
            return 0
    if test and mode == "close":
        today = chart("^GSPC")["rows"][-1][0]          # 测试：用最近一个交易日的收盘
    try:
        items = [analyse(s, n, sh, mode, today) for s, n, sh in INDEXES]
    except LookupError as e:
        print(e)
        if not test:
            state[mode] = str(today); json.dump(state, open(STATE, "w"))
        return 0
    url = webhook_url()
    if not url:
        print("没有配置 MONEY_WEBHOOK_URL", file=sys.stderr)
        return 1
    post(url, embed(mode, today, items, test))
    if not test:
        state[mode] = str(today); json.dump(state, open(STATE, "w"))
    print(mode, today, [(i["short"], round(i["dist"], 1), i["above"]) for i in items])
    return 0


import urllib.parse  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
