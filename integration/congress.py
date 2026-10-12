"""美国国会议员股票交易（STOCK Act 申报）查询：先做成命令行测试，以后可以接到奶妹的 /国会 命令。

数据：Bargo.ai 免费接口（House + Senate，最近 3 个月；不用 Key 每天 30 次 / 100 行）。
条款要求显示来源链接、不能转发原始数据；申报最多晚 45 天，只是信息，不是投资建议。

用法：python integration/congress.py               最新交易
      python integration/congress.py NVDA          某只股票
      python integration/congress.py @nancy-pelosi 某位议员
      python integration/congress.py --top         交易最多的议员
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "https://www.bargo.ai/free-apis/congress/v1"
TYPE = {"purchase": "买入", "buy": "买入", "sale": "卖出", "sell": "卖出", "sale_full": "全部卖出",
        "sale_partial": "部分卖出", "exchange": "置换"}
CHAMBER = {"house": "众议员", "senate": "参议员"}


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "owl-gate-congress-test/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def line(t: dict) -> str:
    typ = TYPE.get(str(t.get("type", "")).lower().replace(" ", "_").replace("(", "").replace(")", ""), t.get("type") or "?")
    who = f"{t.get('member', '?')}（{CHAMBER.get(str(t.get('chamber', '')).lower(), t.get('chamber') or '')}·{t.get('state') or ''}）"
    perf = t.get("perf_pct")
    perf_s = f" · 之后 {perf:+.1f}%" if isinstance(perf, (int, float)) else ""
    return (f"{t.get('transaction_date', '?')}  {who} {typ} **{t.get('ticker') or '—'}** "
            f"{(t.get('asset') or '')[:28]} · 金额 {t.get('amount_range') or '?'}"
            f" · 申报 {t.get('disclosure_date', '?')}{perf_s}")


def main(argv: list[str]) -> str:
    arg = argv[0] if argv else ""
    if arg == "--top":
        d = get("/members?limit=10")
        rows = d.get("members") or d.get("data") or []
        body = "\n".join(f"{i + 1}. {m.get('member') or m.get('name')}（{CHAMBER.get(str(m.get('chamber', '')).lower(), '')}）"
                         f" 交易 {m.get('trades') or m.get('trade_count')} 笔 · 买 {m.get('buys', '?')} / 卖 {m.get('sells', '?')}"
                         f" · 最近 {m.get('last_trade') or m.get('last_trade_date', '?')}" for i, m in enumerate(rows))
        title = "🏛️ 最近 3 个月交易最多的议员"
    else:
        if arg.startswith("@"):
            d = get(f"/members/{arg[1:]}")
            title = f"🏛️ {arg[1:]} 最近 3 个月的交易"
        elif arg:
            d = get(f"/trades/{arg.upper()}?limit=10")
            title = f"🏛️ 议员交易 {arg.upper()}（最新 10 笔）"
        else:
            d = get("/trades?limit=10")
            title = "🏛️ 国会议员最新股票交易（10 笔）"
        trades = d.get("trades") or []
        body = "\n".join(line(t) for t in trades[:10]) or "没有记录"
    return (f"{title}\n{body}\n\n数据：Bargo.ai（House/Senate STOCK Act 申报，最多晚 45 天）https://www.bargo.ai/free-apis/congress"
            f"\n仅供参考，不是投资建议")


if __name__ == "__main__":
    print(main(sys.argv[1:]))
